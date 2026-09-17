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


def mlx_model_status():
    if importlib.util.find_spec("mlx") is None:
        return {"available": False, "reason": "MLX_UNAVAILABLE"}
    # Do not load weights or download packages during an availability check.
    return {"available": True, "reason": None}


def mlx_inference_lock():
    # A cached model keeps this lease until it is released, including across service restarts.
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


def run_mlx(request, invoke, emit, cancelled, worker):
    """Reuse the worker’s weights while keeping prompts and KV caches local to this request."""
    model, tokenizer = worker.load(request["model_id"])
    from mlx_lm import stream_generate
    from mlx_lm.sample_utils import make_sampler

    stream = response = None
    try:
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


class MLXJob:
    def __init__(self, request, invoke):
        self.request = request
        self.invoke = invoke
        self.loop = asyncio.get_running_loop()
        self.events = queue.Queue(maxsize=16)
        self.cancelled = threading.Event()
        self.done = threading.Event()

    def emit(self, event):
        while not self.cancelled.is_set():
            try:
                self.events.put(event, timeout=0.05)
                return
            except queue.Full:
                pass
        raise MLXCancelled

    def call(self, name, arguments):
        future = asyncio.run_coroutine_threadsafe(self.invoke(name, arguments), self.loop)
        try:
            while not self.cancelled.is_set():
                try:
                    return future.result(timeout=0.05)
                except concurrent.futures.TimeoutError:
                    if future.done():
                        raise
            raise MLXCancelled
        finally:
            future.cancel()


class MLXWorker:
    """One persistent worker and one loaded model; overlapping requests are rejected."""

    def __init__(self):
        self.control = threading.Lock()
        self.commands = queue.SimpleQueue()
        self.closed = threading.Event()
        self.closing = False
        self.thread = None
        self.job = None
        self.model = self.tokenizer = self.model_id = None
        self.lease = mlx_inference_lock()
        self.leased = False

    @property
    def busy(self):
        with self.control:
            return self.job is not None

    def load(self, model_id):
        if not self.leased:
            if not self.lease.acquire(blocking=False):
                raise RuntimeError("The previous MLX service is still releasing its model. Try again shortly.")
            self.leased = True
        # Check before reusing weights too: scripts can change the shared package environment.
        prepare_mlx_dependencies()
        if self.model_id == model_id:
            return self.model, self.tokenizer
        if self.model is not None:
            self._drop_model(release_lease=False)
        from mlx_lm import load
        self.model, self.tokenizer = load(model_id, tokenizer_config={"trust_remote_code": False})
        self.model_id = model_id
        return self.model, self.tokenizer

    def _clear_unused(self):
        mx = sys.modules.get("mlx.core")
        if self.leased and mx is not None:
            gc.collect()
            mx.synchronize()
            mx.clear_cache()

    def _drop_model(self, release_lease=True):
        self.model = self.tokenizer = self.model_id = None
        try:
            self._clear_unused()
        finally:
            if release_lease and self.leased:
                self.leased = False
                self.lease.release()

    def _execute(self, job):
        terminal = None
        try:
            if job.cancelled.is_set():
                raise MLXCancelled
            if job.request is None:
                self._drop_model()
                reason = "stop"
            else:
                reason = run_mlx(job.request, job.call, job.emit, job.cancelled, self)
            terminal = {"type": "finish", "reason": reason}
        except MLXCancelled:
            pass
        except Exception as error:
            terminal = {"type": "error", "message": f"{type(error).__name__}: {error}"}
        finally:
            try:
                # Unwind generation frames before clearing KV caches or failed model loads.
                if job.cancelled.is_set() or terminal is None or terminal["type"] == "error":
                    self._drop_model()
                elif job.request is not None:
                    self._clear_unused()
                    mx = sys.modules["mlx.core"]
                    terminal["memory"] = {"peak": mx.get_peak_memory(), "active": mx.get_active_memory(),
                                          "cache": mx.get_cache_memory()}
            except Exception as error:
                terminal = {"type": "error", "message": f"MLX cleanup failed: {error}"}
                self._drop_model()
            finally:
                try:
                    if terminal is not None and not job.cancelled.is_set():
                        job.emit(terminal)
                except MLXCancelled:
                    pass
                finally:
                    with self.control:
                        self.job = None
                    job.done.set()

    def _main(self):
        try:
            while (job := self.commands.get()) is not None:
                self._execute(job)
                job = None
        finally:
            try:
                self._drop_model()
            finally:
                self.closed.set()

    async def events(self, request, invoke):
        job = MLXJob(request, invoke)
        with self.control:
            if self.closing or self.closed.is_set():
                raise RuntimeError("The MLX service has closed")
            if self.job is not None:
                raise RuntimeError("The local model is busy. Wait for the current request to finish and try again.")
            self.job = job
            if self.thread is None:
                self.thread = threading.Thread(target=self._main, name="MLX model", daemon=True)
                try:
                    self.thread.start()
                except Exception:
                    self.thread = None
                    self.job = None
                    raise
            self.commands.put(job)
        try:
            while not job.done.is_set() or not job.events.empty():
                try:
                    event = job.events.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.02)
                    continue
                if event["type"] == "error":
                    raise RuntimeError(event["message"])
                yield event
        finally:
            job.cancelled.set()

    async def release(self):
        if self.thread is not None:
            async for _ in self.events(None, None):
                pass

    def close(self):
        with self.control:
            if self.closing:
                return
            self.closing = True
            if self.job is not None:
                self.job.cancelled.set()
            if self.thread is None:
                self.closed.set()
            else:
                self.commands.put(None)

    async def wait_closed(self):
        while not self.closed.is_set():
            await asyncio.sleep(0.02)
