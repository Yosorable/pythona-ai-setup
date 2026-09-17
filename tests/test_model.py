"""Verify individual SDK schemas and callbacks without requiring Foundation Models."""

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_setup.model import apple_model_events as model_events, native_tools, tool_arguments, tool_schema


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
        sdk.SystemLanguageModel = lambda: SimpleNamespace(is_available=lambda: (True, None))
        sdk.GenerationOptions = SimpleNamespace

        class Session:
            def __init__(self, **kwargs):
                sessions.append(kwargs)
                self.tool = kwargs["tools"][0]

            async def stream_response(self, prompt, options):
                self.assert_prompt = prompt
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
        self.assertEqual([event.get("delta") for event in events], ["Before ", "after", None])
