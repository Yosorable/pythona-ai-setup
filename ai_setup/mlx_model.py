"""Run MLX-LM in a worker, using the model's chat template and tool parser."""

import asyncio
import concurrent.futures
import copy
import gc
import importlib.util
import json
import queue
import sys
import threading
import types
import uuid


MLX_LM_VERSION = "0.31.3"


def mlx_model_status():
    if importlib.util.find_spec("mlx") is None:
        return {"available": False, "reason": "MLX_UNAVAILABLE"}
    # Do not load weights or download packages during an availability check.
    return {"available": True, "reason": None}


def mlx_inference_lock():
    # All embedded backend copies share only a lock, never cached model weights.
    state = types.ModuleType("_pythona_ai_setup_mlx")
    state.lock = threading.Lock()
    return sys.modules.setdefault(state.__name__, state).lock


class MLXCancelled(Exception):
    pass


class MLXOutput:
    """Keep partial markers and tool arguments out of user-visible text."""

    def __init__(self, tokenizer, definitions):
        self.tokenizer = tokenizer
        self.definitions = definitions
        self.buffer = ""
        self.mode = "text"
        self.calls = []
        self.start = tokenizer.tool_call_start if tokenizer.has_tool_calling else None
        self.end = tokenizer.tool_call_end if tokenizer.has_tool_calling else None
        self.think_start = tokenizer.think_start if tokenizer.has_thinking else None
        self.think_end = tokenizer.think_end if tokenizer.has_thinking else None

    def _tool(self, text):
        parsed = self.tokenizer.tool_parser(text, self.definitions)
        values = parsed if isinstance(parsed, list) else [parsed]
        by_name = {tool["function"]["name"]: tool["function"]["parameters"] for tool in self.definitions}
        for call in values:
            if not isinstance(call, dict) or call.get("name") not in by_name:
                raise ValueError("The model returned a disabled or unknown tool")
            arguments = call.get("arguments")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            arguments = tool_arguments(by_name[call["name"]], json.dumps(arguments, ensure_ascii=False))
            self.calls.append({"id": "mlx_" + uuid.uuid4().hex, "type": "function",
                               "function": {"name": call["name"], "arguments": arguments}})

    def feed(self, text, final=False):
        self.buffer += text
        output = ""
        while self.buffer:
            if self.mode != "text":
                ending = self.end if self.mode == "tool" else self.think_end
                index = self.buffer.find(ending) if ending else -1
                if index < 0:
                    if final:
                        if self.mode == "tool" and not ending:
                            self._tool(self.buffer)
                            self.buffer = ""
                            self.mode = "text"
                            break
                        raise ValueError("The model returned an incomplete tool call or reasoning block")
                    break
                if self.mode == "tool":
                    self._tool(self.buffer[:index])
                self.buffer = self.buffer[index + len(ending):]
                self.mode = "text"
                continue
            markers = [(marker, mode) for marker, mode in ((self.start, "tool"), (self.think_start, "thinking")) if marker]
            matches = [(self.buffer.find(marker), marker, mode) for marker, mode in markers if marker in self.buffer]
            if matches:
                index, marker, self.mode = min(matches)
                output += self.buffer[:index]
                self.buffer = self.buffer[index + len(marker):]
                continue
            keep = 0
            if not final:
                for marker, _ in markers:
                    for length in range(1, min(len(marker), len(self.buffer) + 1)):
                        if self.buffer.endswith(marker[:length]):
                            keep = max(keep, length)
            if keep:
                output += self.buffer[:-keep]
                self.buffer = self.buffer[-keep:]
            else:
                output += self.buffer
                self.buffer = ""
            break
        if final and self.mode != "text":
            raise ValueError("The model returned an incomplete tool call or reasoning block")
        return output


def mlx_messages(request):
    messages = []
    if request["instructions"]:
        messages.append({"role": "system", "content": request["instructions"]})
    for original in request["messages"]:
        message = {"role": original["role"], "content": original.get("content", "")}
        if original["role"] == "assistant" and original.get("tool_calls"):
            calls = []
            for call in original["tool_calls"]:
                arguments = call["arguments"]
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                calls.append({"id": call["id"], "type": "function",
                              "function": {"name": call["name"], "arguments": arguments}})
            message["tool_calls"] = calls
        if original["role"] == "tool":
            message.update(name=original["name"], tool_call_id=original["tool_call_id"])
            message["content"] = json.dumps({"content": message["content"], "failed": original.get("failed", False)}, ensure_ascii=False)
        messages.append(message)
    return messages


def run_mlx(request, invoke, emit, cancelled):
    """Own all model objects on this worker and release them before returning."""
    if importlib.util.find_spec("mlx_lm") is None:
        from pythona import packages
        packages.install("mlx-lm", version=MLX_LM_VERSION)
    if cancelled.is_set():
        raise MLXCancelled
    from mlx_lm import load, stream_generate
    from mlx_lm.sample_utils import make_sampler

    model = tokenizer = stream = response = None
    try:
        # Loading by repository ID uses Hugging Face's download cache.
        model, tokenizer = load(request["model_id"], tokenizer_config={"trust_remote_code": False})
        if cancelled.is_set():
            raise MLXCancelled
        if not tokenizer.has_chat_template:
            raise ValueError("This MLX model has no chat template")
        if request["tools"] and not tokenizer.has_tool_calling:
            raise ValueError("This MLX model does not support tool calling; disable tools or choose another model")
        definitions = [{"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""),
                        "parameters": copy.deepcopy(tool["inputSchema"])}} for tool in request["tools"]]
        messages = mlx_messages(request)
        for _ in range(16):
            if cancelled.is_set():
                raise MLXCancelled
            prompt = tokenizer.apply_chat_template(messages, tools=definitions or None, tokenize=True,
                                                   add_generation_prompt=True, enable_thinking=False)
            parser = MLXOutput(tokenizer, definitions)
            text = ""
            reason = None
            stream = stream_generate(model, tokenizer, prompt=prompt, max_tokens=request["maximum_response_tokens"],
                                     sampler=make_sampler(temp=0.0))
            try:
                for response in stream:
                    if cancelled.is_set():
                        raise MLXCancelled
                    delta = parser.feed(response.text)
                    if delta:
                        text += delta
                        emit({"type": "text", "delta": delta})
                    if response.finish_reason is not None:
                        reason = response.finish_reason
                delta = parser.feed("", final=True)
                if delta:
                    text += delta
                    emit({"type": "text", "delta": delta})
            finally:
                stream.close()
                stream = response = None
            if reason is None:
                raise RuntimeError("The MLX model stream ended without a finish reason")
            if not parser.calls:
                if not text.strip():
                    raise RuntimeError("The model returned no response text")
                return reason
            if reason != "stop":
                raise ValueError("The model reached its token limit while generating tool calls")
            # Validate every call before executing any of them, then preserve all results.
            messages.append({"role": "assistant", "content": text, "tool_calls": parser.calls})
            for call in parser.calls:
                function = call["function"]
                result = invoke(function["name"], function["arguments"])
                messages.append({"role": "tool", "name": function["name"], "tool_call_id": call["id"], "content": result})
        raise RuntimeError("The MLX model exceeded the tool round limit")
    finally:
        if stream is not None:
            stream.close()
        stream = response = model = tokenizer = None


async def mlx_model_events(request, invoke):
    # A synchronous MLX kernel or download must never occupy the HTTP event loop.
    loop = asyncio.get_running_loop()
    cancelled = threading.Event()
    events = queue.Queue(maxsize=16)

    def emit(event):
        while not cancelled.is_set():
            try:
                events.put(event, timeout=0.05)
                return
            except queue.Full:
                pass
        raise MLXCancelled

    def call(name, arguments):
        future = asyncio.run_coroutine_threadsafe(invoke(name, arguments), loop)
        try:
            while not cancelled.is_set():
                try:
                    return future.result(timeout=0.05)
                except concurrent.futures.TimeoutError:
                    pass
            raise MLXCancelled
        finally:
            future.cancel()

    def worker():
        lock = mlx_inference_lock()
        acquired = False
        terminal = None
        try:
            while not cancelled.is_set():
                if lock.acquire(timeout=0.05):
                    acquired = True
                    break
            if not acquired or cancelled.is_set():
                raise MLXCancelled
            reason = run_mlx(request, call, emit, cancelled)
            terminal = {"type": "finish", "reason": reason}
        except MLXCancelled:
            pass
        except Exception as error:
            terminal = {"type": "error", "message": f"{type(error).__name__}: {error}"}
        finally:
            if acquired:
                try:
                    # Clean up after exception tracebacks have released model references too.
                    mx = sys.modules.get("mlx.core")
                    if mx is not None:
                        gc.collect()
                        mx.synchronize()
                        mx.clear_cache()
                        if terminal is not None and terminal["type"] == "finish":
                            # Peak is process-wide, not a per-request allocation.
                            terminal["memory"] = {"peak": mx.get_peak_memory(), "active": mx.get_active_memory(),
                                                  "cache": mx.get_cache_memory()}
                except Exception as error:
                    terminal = {"type": "error", "message": f"MLX cleanup failed: {error}"}
                finally:
                    lock.release()
        if terminal is not None and not cancelled.is_set():
            try:
                emit(terminal)
            except MLXCancelled:
                pass

    thread = threading.Thread(target=worker, name="MLX inference", daemon=True)
    thread.start()
    try:
        while thread.is_alive() or not events.empty():
            try:
                event = events.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            if event["type"] == "error":
                raise RuntimeError(event["message"])
            yield event
    finally:
        # The worker keeps its lock until an in-flight native call returns and cleanup finishes.
        cancelled.set()
