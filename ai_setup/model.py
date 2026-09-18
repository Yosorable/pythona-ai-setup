"""Use independent Foundation Models tools and resume generation with native results."""

import asyncio
import json
import math
from typing import Optional


def apple_model_status():
    try:
        import apple_fm_sdk as fm
        model = fm.SystemLanguageModel()
        available, reason = model.is_available()
        return {"available": available, "reason": reason.name if reason is not None else None}
    except Exception as error:
        return {"available": False, "reason": f"{type(error).__name__}: {error}"}


PARAMETER_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


def tool_schema(fm, definition):
    """Map the App's flat parameter schemas to typed, individually named SDK tools."""
    schema = definition["inputSchema"]
    if schema.get("type") != "object" or set(schema) - {
        "type", "properties", "required", "additionalProperties", "description", "title"
    }:
        raise ValueError(f"Unsupported tool schema: {definition['name']}")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if (not isinstance(properties, dict) or not isinstance(required, list)
            or any(not isinstance(key, str) or key not in properties for key in required)):
        raise ValueError("Invalid required tool parameters")
    fields = []
    for name, parameter in properties.items():
        if not isinstance(parameter, dict) or set(parameter) - {"type", "description", "enum", "minimum", "maximum"}:
            raise ValueError(f"Unsupported schema for {definition['name']}.{name}")
        kind = parameter.get("type")
        if kind not in PARAMETER_TYPES:
            raise ValueError(f"Unsupported parameter type: {definition['name']}.{name}")
        guides = []
        if "enum" in parameter:
            choices = parameter["enum"]
            if kind != "string" or not isinstance(choices, list) or not choices or any(type(v) is not str for v in choices):
                raise ValueError("Only string enums are supported")
            guides.append(fm.GenerationGuide.anyOf(choices))
        for bound in ("minimum", "maximum"):
            if bound in parameter:
                value = parameter[bound]
                if kind not in ("integer", "number") or type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError(f"Invalid {bound} constraint")
                guides.append(getattr(fm.GenerationGuide, bound)(value))
        value_type = PARAMETER_TYPES[kind]
        if name not in required:
            value_type = Optional[value_type]
        fields.append(fm.generation_property.Property(name=name, type_class=value_type,
                                                      description=parameter.get("description"), guides=guides))
    arguments_type = type(definition["name"] + "_arguments", (), {})
    return fm.GenerationSchema(type_class=arguments_type, properties=fields,
                               description=schema.get("description"))


def tool_arguments(schema, content):
    """Validate complete arguments before native execution; omit absent optional fields."""
    value = json.loads(content)
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(value, dict) or set(value) - set(properties) or any(key not in value for key in required):
        raise ValueError("Invalid tool argument object")
    result = {}
    for name, item in value.items():
        if item is None and name not in required:
            continue
        parameter = properties[name]
        kind = parameter["type"]
        valid = (type(item) in (int, float) and math.isfinite(item)) if kind == "number" else type(item) is PARAMETER_TYPES[kind]
        if not valid or ("enum" in parameter and item not in parameter["enum"]):
            raise ValueError(f"Invalid tool parameter: {name}")
        if ("minimum" in parameter and item < parameter["minimum"]) or ("maximum" in parameter and item > parameter["maximum"]):
            raise ValueError(f"Tool parameter out of range: {name}")
        result[name] = item
    return result


def native_tools(fm, definitions, invoke):
    loop = asyncio.get_running_loop()

    class NativeTool(fm.Tool):
        def __init__(self, definition):
            self.definition = definition
            self.name = definition["name"]
            self.description = definition.get("description", "")
            self.schema = tool_schema(fm, definition)
            super().__init__()

        @property
        def arguments_schema(self):
            return self.schema

        async def call(self, args):
            arguments = tool_arguments(self.definition["inputSchema"], args.to_json())
            try:
                # SDK callbacks may use a separate thread and asyncio loop.
                if asyncio.get_running_loop() is loop:
                    return await invoke(self.name, arguments)
                coroutine = invoke(self.name, arguments)
                try:
                    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                except RuntimeError:
                    coroutine.close()
                    raise RuntimeError("The model service has closed") from None
                return await asyncio.wrap_future(future)
            except asyncio.CancelledError:
                # The SDK catches Exception, not BaseException, to finish its C callback.
                raise RuntimeError("The model request was cancelled") from None

    return [NativeTool(definition) for definition in definitions]


def apple_history_entries(messages):
    """Translate completed messages into the SDK's version 1 Transcript format."""
    entries = []
    pending = {}

    def text_entry(role, identifier, content):
        return {"role": role, "id": identifier,
                "contents": [{"type": "text", "id": identifier + "-text", "text": content}]}

    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not isinstance(message.get("content", ""), str):
            raise ValueError("Invalid Apple model conversation message")
        role = message.get("role")
        content = message.get("content", "")
        identifier = f"history-{index}"
        if role == "user":
            if pending:
                raise ValueError("Apple model history has a tool call without a result")
            entry = text_entry("user", identifier, content)
            entry["options"] = {}
            entries.append(entry)
        elif role == "assistant":
            if content:
                entries.append(text_entry("response", identifier, content))
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                raise ValueError("Invalid historical tool calls")
            translated = []
            for call_index, call in enumerate(calls):
                if (not isinstance(call, dict)
                        or any(not isinstance(call.get(key), str) or not call[key] for key in ("id", "name"))
                        or not isinstance(call.get("arguments"), str)):
                    raise ValueError("Invalid historical tool call")
                if call["id"] in pending:
                    raise ValueError("Duplicate pending historical tool call")
                arguments = json.loads(call["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Historical tool arguments must be an object")
                # App call IDs may be reused in different turns. Keep each native pair unique.
                call_id = f"{identifier}-call-{call_index}"
                pending[call["id"]] = (call_id, call["name"])
                translated.append({"id": call_id, "name": call["name"],
                                   "arguments": json.dumps(arguments, ensure_ascii=False, allow_nan=False)})
            if translated:
                # Native text responses and tool-call batches are separate transcript entries.
                entries.append({"role": "response", "id": identifier + "-calls", "toolCalls": translated})
        elif role == "tool":
            external_id = message.get("tool_call_id")
            if not isinstance(external_id, str) or not external_id:
                raise ValueError("Invalid historical tool result ID")
            call = pending.pop(external_id, None)
            if call is None or call[1] != message.get("name"):
                raise ValueError("Historical tool result has no matching call")
            failed = message.get("failed", False)
            if type(failed) is not bool:
                raise ValueError("Invalid historical tool failure flag")
            # Match ModelRun.invoke's result envelope, including the native failure flag.
            output = json.dumps({"content": content, "failed": failed}, ensure_ascii=False)
            entry = text_entry("tool", call[0], output)
            entry.update(toolCallID=call[0], toolName=call[1])
            entries.append(entry)
        else:
            raise ValueError(f"Unsupported Apple model history role: {role}")
    if pending:
        raise ValueError("Apple model history has a tool call without a result")
    return entries


async def apple_session(fm, request, model, tools):
    """Restore prior turns without regenerating answers or executing historical tools."""
    messages = request["messages"]
    if (not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict)
            or messages[-1].get("role") != "user" or not isinstance(messages[-1].get("content"), str)):
        raise ValueError("A new Apple model request must end with a user message")
    history = apple_history_entries(messages[:-1])
    session = fm.LanguageModelSession(model=model, instructions=request["instructions"], tools=tools)
    if history:
        # Let the SDK serialize current instructions and typed tool definitions itself.
        serialized = await session.transcript.to_dict()
        if serialized.get("version") != 1 or serialized.get("type") != "FoundationModels.Transcript":
            raise ValueError("Unsupported Foundation Models transcript format")
        serialized["transcript"]["entries"].extend(history)
        transcript = await fm.Transcript.from_dict(serialized)
        session = fm.LanguageModelSession.from_transcript(transcript, model=model, tools=tools)
    return session, messages[-1]["content"]


async def apple_model_events(request, invoke):
    # Lazy loading keeps installation usable without a working SDK or model.
    import apple_fm_sdk as fm

    model = fm.SystemLanguageModel()
    available, reason = model.is_available()
    if not available:
        raise RuntimeError(f"Apple on-device model unavailable: {reason.name if reason is not None else 'unknown'}")
    tools = native_tools(fm, request["tools"], invoke)
    session, prompt = await apple_session(fm, request, model, tools)
    options = fm.GenerationOptions(maximum_response_tokens=request["maximum_response_tokens"])
    previous = ""
    stream = session.stream_response(prompt, options=options)
    try:
        async for snapshot in stream:
            if not snapshot.startswith(previous):
                raise RuntimeError("The model rewrote text that was already streamed")
            delta = snapshot[len(previous):]
            previous = snapshot
            if delta:
                yield {"type": "text", "delta": delta}
    finally:
        await stream.aclose()
    if not previous.strip():
        raise RuntimeError("The model returned no response text")
    yield {"type": "finish", "reason": "stop"}


def model_status(settings):
    return mlx_model_status() if settings["backend"] == "mlx_lm" else apple_model_status()


async def model_events(request, invoke, mlx):
    apple_lease = False
    if request["backend"] == "mlx_lm":
        events = mlx.events(request, invoke)
    else:
        await mlx.release()
        if not mlx.lease.acquire(blocking=False):
            raise RuntimeError("The previous model service is still stopping. Try again shortly.")
        apple_lease = True
        events = apple_model_events(request, invoke)
    try:
        async for event in events:
            yield event
    finally:
        try:
            await events.aclose()
        finally:
            if apple_lease:
                mlx.lease.release()
