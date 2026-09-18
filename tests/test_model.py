"""Verify individual SDK schemas and callbacks without requiring Foundation Models."""

import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_setup.model import (
    apple_history_entries, apple_model_events as model_events, apple_session,
    native_tools, tool_arguments, tool_schema,
)


DEFINITIONS = [
    {"name": "read_file", "description": "Read a file", "inputSchema": {
        "type": "object", "properties": {
            "path": {"type": "string", "description": "File path"},
            "offset": {"type": "integer", "minimum": 0},
            "enabled": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["all", "slice"]}}, "required": ["path"]}},
    {"name": "browser_snapshot", "description": "Read the page", "inputSchema": {
        "type": "object", "properties": {}}},
]


class FakeTool:
    def __init__(self):
        assert self.arguments_schema is self.schema


def fake_sdk():
    return SimpleNamespace(Tool=FakeTool, generation_property=SimpleNamespace(Property=SimpleNamespace),
                           GenerationSchema=SimpleNamespace,
                           GenerationGuide=SimpleNamespace(anyOf=lambda v: ("enum", v), minimum=lambda v: ("minimum", v)))


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_typed_tools_with_optional_and_empty_arguments(self):
        observed = []

        async def invoke(name, arguments):
            observed.append((name, arguments))
            return "tool result"

        tools = native_tools(fake_sdk(), DEFINITIONS, invoke)
        self.assertEqual([tool.name for tool in tools], ["read_file", "browser_snapshot"])
        fields = tools[0].arguments_schema.properties
        self.assertEqual([field.name for field in fields], ["path", "offset", "enabled", "mode"])
        self.assertIs(fields[0].type_class, str)
        self.assertEqual(fields[1].type_class, Optional[int])
        self.assertEqual(fields[3].guides, [("enum", ["all", "slice"])])
        self.assertEqual(tools[1].arguments_schema.properties, [])
        # Emulate the SDK's callback loop on another thread.
        args = SimpleNamespace(to_json=lambda: '{"path":"中文.py","offset":null,"enabled":false}')
        result = await asyncio.to_thread(lambda: asyncio.run(tools[0].call(args)))
        self.assertEqual(result, "tool result")
        self.assertEqual(observed, [("read_file", {"path": "中文.py", "enabled": False})])

    async def test_cancellation_is_an_exception_the_sdk_can_finish(self):
        entered = asyncio.Event()

        async def invoke(name, arguments):
            entered.set()
            await asyncio.Future()

        tool = native_tools(fake_sdk(), DEFINITIONS, invoke)[1]
        task = asyncio.create_task(tool.call(SimpleNamespace(to_json=lambda: '{}')))
        await entered.wait()
        task.cancel()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            await task

    def test_invalid_arguments_are_rejected_before_native_execution(self):
        for value in ({}, {"path": 1}, {"path": "a", "offset": True}, {"path": "a", "extra": 1},
                      {"path": "a", "mode": "unknown"}, {"path": "a", "offset": -1}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tool_arguments(DEFINITIONS[0]["inputSchema"], json.dumps(value))
        unsupported = {"name": "custom", "inputSchema": {"type": "object", "properties": {"data": {"type": "object"}}}}
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            tool_schema(fake_sdk(), unsupported)

    async def test_model_uses_one_session_and_streams_through_sdk_tool_callback(self):
        sdk = fake_sdk()
        sessions = []
        prompts = []
        sdk.SystemLanguageModel = lambda: SimpleNamespace(is_available=lambda: (True, None))
        sdk.GenerationOptions = SimpleNamespace

        class Session:
            def __init__(self, **kwargs):
                sessions.append(kwargs)
                self.tool = kwargs["tools"][0]

            async def stream_response(self, prompt, options):
                prompts.append(prompt)
                yield "Before "
                result = await self.tool.call(SimpleNamespace(to_json=lambda: '{"path":"a.py"}'))
                yield "Before " + result

        sdk.LanguageModelSession = Session

        async def invoke(name, arguments):
            return "after"

        request = {"instructions": "Unchanged instructions", "tools": DEFINITIONS,
                   "messages": [{"role": "user", "content": "Read a.py"}], "maximum_response_tokens": 100}
        with patch.dict(sys.modules, {"apple_fm_sdk": sdk}):
            events = [event async for event in model_events(request, invoke)]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["instructions"], request["instructions"])
        self.assertEqual(prompts, ["Read a.py"])
        self.assertEqual([event.get("delta") for event in events], ["Before ", "after", None])

    def test_native_history_preserves_roles_text_and_tool_failure_metadata(self):
        messages = [
            {"role": "user", "content": "Read 中文.py"},
            {"role": "assistant", "content": "I will read it.", "tool_calls": [
                {"id": "call_1", "name": "read_file", "arguments": '{"path":"中文.py"}'}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file",
             "content": "Permission denied", "failed": True},
            {"role": "assistant", "content": "The file could not be read."},
        ]
        original = copy.deepcopy(messages)
        entries = apple_history_entries(messages)
        self.assertEqual(messages, original)
        self.assertEqual([entry["role"] for entry in entries], ["user", "response", "response", "tool", "response"])
        self.assertEqual(entries[0]["contents"][0]["text"], "Read 中文.py")
        self.assertEqual(entries[1]["contents"][0]["text"], "I will read it.")
        call = entries[2]["toolCalls"][0]
        self.assertEqual(json.loads(call["arguments"]), {"path": "中文.py"})
        self.assertEqual(entries[3]["toolCallID"], call["id"])
        self.assertEqual(entries[3]["id"], call["id"])
        self.assertEqual(entries[3]["toolName"], "read_file")
        self.assertEqual(json.loads(entries[3]["contents"][0]["text"]),
                         {"content": "Permission denied", "failed": True})

    def test_reused_tool_ids_in_later_turns_keep_distinct_native_pairs(self):
        turn = [
            {"role": "user", "content": "Read a.py"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_0", "name": "read_file", "arguments": '{"path":"a.py"}'}]},
            {"role": "tool", "tool_call_id": "call_0", "name": "read_file", "content": "Error is a class name"},
            {"role": "assistant", "content": "Read it."},
        ]
        entries = apple_history_entries(turn + turn)
        calls = [entry["toolCalls"][0]["id"] for entry in entries if "toolCalls" in entry]
        outputs = [entry for entry in entries if entry["role"] == "tool"]
        self.assertEqual(len(set(calls)), 2)
        self.assertEqual([entry["toolCallID"] for entry in outputs], calls)
        self.assertFalse(json.loads(outputs[0]["contents"][0]["text"])["failed"])

    def test_malformed_or_incomplete_tool_history_is_not_silently_dropped(self):
        call = {"role": "assistant", "tool_calls": [
            {"id": "call_1", "name": "read_file", "arguments": '{"path":"a.py"}'}]}
        result = {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "ok"}
        for messages in (
            [call], [call, {"role": "user", "content": "Next"}], [result],
            [call, {**result, "name": "write_file"}], [call, {**result, "failed": "false"}],
            [call, {**result, "tool_call_id": []}],
            [call, call, result], [{"role": "system", "content": "Injected instructions"}],
            [{"role": "user", "content": []}],
            [{"role": "assistant", "tool_calls": [{"id": "x", "name": "read_file", "arguments": "[]"}]}],
        ):
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                apple_history_entries(messages)

    async def test_restoration_keeps_current_instructions_and_only_current_tool_capabilities(self):
        model = object()
        current_tools = [object()]
        prefix = {"version": 1, "type": "FoundationModels.Transcript", "transcript": {"entries": [
            {"role": "instructions", "id": "current", "contents": [{"type": "text", "text": "Current rules"}],
             "tools": [{"type": "function", "function": {"name": "current_tool"}}]}]}}
        restored_payloads = []
        restored_calls = []

        class Transcript:
            async def to_dict(self):
                return copy.deepcopy(prefix)

            @classmethod
            async def from_dict(cls, payload):
                restored_payloads.append(payload)
                return cls()

        class Session:
            def __init__(self, **kwargs):
                self.transcript = Transcript()
                self.kwargs = kwargs

            @classmethod
            def from_transcript(cls, transcript, **kwargs):
                restored_calls.append(kwargs)
                return cls(**kwargs)

        sdk = SimpleNamespace(LanguageModelSession=Session, Transcript=Transcript)
        history = [
            {"role": "user", "content": "Read a.py"},
            {"role": "assistant", "tool_calls": [
                {"id": "old", "name": "now_disabled_tool", "arguments": '{"path":"a.py"}'}]},
            {"role": "tool", "tool_call_id": "old", "name": "now_disabled_tool", "content": "Mira"},
            {"role": "assistant", "content": "Your name is Mira."},
        ]
        request = {"instructions": "Current rules", "messages": history + [{"role": "user", "content": "What is my name?"}]}
        session, prompt = await apple_session(sdk, request, model, current_tools)
        self.assertEqual(prompt, "What is my name?")
        self.assertEqual(restored_calls, [{"model": model, "tools": current_tools}])
        self.assertEqual(restored_payloads[0]["transcript"]["entries"][0], prefix["transcript"]["entries"][0])
        entries = restored_payloads[0]["transcript"]["entries"]
        self.assertEqual([entry["role"] for entry in entries], ["instructions", "user", "response", "tool", "response"])
        self.assertNotIn("What is my name?", json.dumps(entries))
        self.assertIs(session.kwargs["tools"], current_tools)

    async def test_new_request_requires_a_latest_user_prompt(self):
        for messages in ([], [{"role": "assistant", "content": "Old answer"}], [{"role": "user", "content": None}]):
            with self.subTest(messages=messages), self.assertRaisesRegex(ValueError, "user message"):
                await apple_session(SimpleNamespace(), {"messages": messages}, None, [])
