"""Exercise MLX streaming, tool schemas, cleanup, and cancellation without weights."""

import asyncio
import copy
import http.client
import importlib.machinery
import json
import sys
import threading
import types
import unittest
from unittest.mock import patch
import weakref

from ai_setup.bundle import load_backend
from ai_setup.settings import service_defaults


READ_FILE = {"name": "read_file", "description": "Read a file", "inputSchema": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}


class Tokenizer:
    has_chat_template = True
    has_tool_calling = True
    has_thinking = True
    tool_call_start = '<tool_call>'
    tool_call_end = '</tool_call>'
    think_start = '<think>'
    think_end = '</think>'
    def __init__(self):
        self.prompts = []
    def apply_chat_template(self, messages, **options):
        self.prompts.append((copy.deepcopy(messages), copy.deepcopy(options)))
        return [1, 2, 3]
    @staticmethod
    def tool_parser(text, tools):
        return json.loads(text)


class MLXTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.namespace = load_backend()
        self.worker = self.namespace["MLXWorker"]()
        self.workers = [self.worker]
        self.services = []
        self.tokenizer = Tokenizer()
        self.models = []
        self.cleanup_calls = []
        self.active = 0
        self.peak_active = 0
        self.chunks = ['Hello 😀']
        self.loads = []
        self.threads = []
        self.request = {'backend': 'mlx_lm', 'model_id': 'mlx-community/Qwen3-1.7B-4bit',
                        'instructions': 'Keep these instructions', 'messages': [{'role': 'user', 'content': 'Hi'}],
                        'tools': [], 'maximum_response_tokens': 64}
        self.mx = types.ModuleType('mlx.core')
        self.mx.synchronize = lambda: self.cleanup_calls.append('synchronize')
        self.mx.clear_cache = lambda: self.cleanup_calls.append('clear_cache')
        self.mx.get_peak_memory = lambda: 300
        self.mx.get_active_memory = lambda: self.active * 100
        self.mx.get_cache_memory = lambda: 0
        self.mlx = types.ModuleType('mlx')
        self.mlx.__path__ = []
        self.mlx.core = self.mx
        self.lm = types.ModuleType('mlx_lm')
        self.lm.load = self.load
        self.lm.stream_generate = self.generate
        self.sampling = types.ModuleType('mlx_lm.sample_utils')
        self.sampling.make_sampler = lambda **options: options
        self.patch_modules = patch.dict(sys.modules, {'mlx': self.mlx, 'mlx.core': self.mx,
            'mlx_lm': self.lm, 'mlx_lm.sample_utils': self.sampling})
        self.patch_modules.start()
        self.addCleanup(self.patch_modules.stop)
        spec = patch('importlib.util.find_spec', return_value=importlib.machinery.ModuleSpec('mlx_lm', None))
        spec.start()
        self.addCleanup(spec.stop)

    def load(self, model_id, **options):
        class Model:
            pass
        model = Model()
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        weakref.finalize(model, self.released)
        self.models.append(weakref.ref(model))
        self.loads.append((model_id, options))
        return model, self.tokenizer

    def released(self):
        self.active -= 1

    def generate(self, model, tokenizer, **options):
        self.threads.append(threading.get_ident())
        text = self.chunks.pop(0)
        # Splitting every character exercises partial tool and reasoning markers.
        for char in text:
            yield types.SimpleNamespace(text=char, finish_reason=None)
        yield types.SimpleNamespace(text='', finish_reason='stop')

    async def collect(self, invoke=None):
        async def unavailable(name, arguments):
            self.fail('Unexpected native tool call')
        return [event async for event in self.worker.events(self.request, invoke or unavailable)]

    async def asyncTearDown(self):
        for service in self.services:
            service.stop()
        for worker in self.workers:
            worker.close()
        for worker in self.workers:
            await asyncio.wait_for(worker.wait_closed(), 3)
        for service in self.services:
            self.assertTrue(await asyncio.to_thread(service.closed.wait, 3))
        self.assertEqual(self.active, 0)
        self.assertTrue(all(model() is None for model in self.models))

    async def wait_idle(self):
        for _ in range(150):
            if not self.worker.busy:
                return
            await asyncio.sleep(0.02)
        self.fail("The MLX worker did not finish cleanup")

    async def test_chat_template_streaming_and_cleanup_metrics(self):
        events = await self.collect()
        self.assertEqual(''.join(event.get('delta', '') for event in events), 'Hello 😀')
        self.assertEqual(self.loads[0], (self.request['model_id'], {'tokenizer_config': {'trust_remote_code': False}}))
        self.assertEqual(self.tokenizer.prompts[0][0][0], {'role': 'system', 'content': 'Keep these instructions'})
        self.assertFalse(self.tokenizer.prompts[0][1]['enable_thinking'])
        self.assertEqual(events[-1]['memory']['active'], 100)
        self.assertTrue(all(model() is not None for model in self.models))
        self.assertEqual(self.cleanup_calls, ['synchronize', 'clear_cache'])

    async def test_tool_calls_use_individual_schemas_and_continue_with_native_results(self):
        self.request['tools'] = [READ_FILE]
        self.chunks = ['<think>Hidden reasoning</think>Reading <tool_call>{"name":"read_file","arguments":{"path":"a.py"}}</tool_call>', 'Result 😀']
        calls = []
        async def invoke(name, arguments):
            calls.append((name, arguments))
            return json.dumps({'content': 'Permission denied', 'failed': True})
        events = await self.collect(invoke)
        self.assertEqual(calls, [('read_file', {'path': 'a.py'})])
        self.assertEqual(''.join(event.get('delta', '') for event in events), 'Reading Result 😀')
        messages, options = self.tokenizer.prompts[1]
        self.assertEqual(options['tools'][0]['function']['parameters'], READ_FILE['inputSchema'])
        self.assertEqual(messages[-2]['tool_calls'][0]['function']['arguments'], {'path': 'a.py'})
        self.assertTrue(json.loads(messages[-1]['content'])['failed'])
        self.assertEqual(messages[-1]['tool_call_id'], messages[-2]['tool_calls'][0]['id'])
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(self.active, 1)

    async def test_malformed_or_disabled_tool_never_executes_and_releases_model(self):
        for text in ['<tool_call>{"name":"read_file","arguments":{"path":7}}</tool_call>',
                     '<tool_call>{"name":"write_file","arguments":{}}</tool_call>',
                     '<tool_call>{"name":"read_file"']:
            self.request['tools'] = [READ_FILE]
            self.chunks = [text]
            with self.assertRaises((RuntimeError, ValueError)):
                await self.collect()
            self.assertEqual(self.active, 0)

    async def test_cancellation_during_load_rejects_another_request_until_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        original_load = self.load
        count = 0
        def blocked_load(*args, **kwargs):
            nonlocal count
            count += 1
            result = original_load(*args, **kwargs)
            if count == 1:
                entered.set()
                release.wait(3)
            return result
        self.lm.load = blocked_load
        first = asyncio.create_task(self.collect())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        try:
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                await self.collect()
            self.assertEqual(count, 1)
        finally:
            release.set()
        await self.wait_idle()
        self.assertEqual(self.active, 0)
        events = await asyncio.wait_for(self.collect(), 3)
        self.assertEqual(events[-1]['reason'], 'stop')
        self.assertEqual(count, 2)
        self.assertEqual(self.peak_active, 1)
        self.assertEqual(self.active, 1)

    async def test_no_tool_support_fails_before_generation(self):
        self.tokenizer.has_tool_calling = False
        self.request['tools'] = [READ_FILE]
        with self.assertRaisesRegex(RuntimeError, 'does not support tool calling'):
            await self.collect()
        self.assertEqual(self.active, 0)

    async def test_reuses_weights_on_one_thread_without_reusing_conversation_state(self):
        self.chunks = ['First answer', 'Second answer']
        await self.collect()
        self.request['instructions'] = 'Different instructions'
        self.request['messages'] = [{'role': 'user', 'content': 'Independent conversation'}]
        await self.collect()
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(len(set(self.threads)), 1)
        self.assertEqual(self.tokenizer.prompts[1][0], [
            {'role': 'system', 'content': 'Different instructions'},
            {'role': 'user', 'content': 'Independent conversation'}])
        self.assertEqual(self.active, 1)

    async def test_switching_model_releases_old_weights_before_loading_next(self):
        self.chunks = ['First', 'Second']
        await self.collect()
        original = self.models[0]
        self.request['model_id'] = 'example/another-model'
        await self.collect()
        self.assertIsNone(original())
        self.assertEqual(len(self.loads), 2)
        self.assertEqual(self.peak_active, 1)
        self.assertEqual(self.worker.model_id, 'example/another-model')

    async def test_release_drops_model_and_next_request_loads_it_again(self):
        self.chunks = ['First', 'Second']
        await self.collect()
        await self.worker.release()
        self.assertEqual(self.active, 0)
        self.assertFalse(self.worker.lease.locked())
        await self.collect()
        self.assertEqual(len(self.loads), 2)

    async def test_another_backend_copy_cannot_load_while_a_model_is_retained(self):
        self.chunks = ['First', 'Second']
        await self.collect()
        other = load_backend()['MLXWorker']()
        self.workers.append(other)
        async def unused(*args):
            self.fail('Unexpected tool')
        with self.assertRaisesRegex(RuntimeError, 'previous MLX service'):
            _ = [event async for event in other.events(self.request, unused)]
        self.assertEqual(len(self.loads), 1)
        self.worker.close()
        await self.worker.wait_closed()
        _ = [event async for event in other.events(self.request, unused)]
        self.assertEqual(len(self.loads), 2)
        self.assertEqual(self.peak_active, 1)

    def service(self, **settings):
        config = {**service_defaults(), 'port': 0, **settings}
        service = self.namespace['LocalModelService'](config, 'test-build').start()
        self.services.append(service)
        self.workers.append(service.mlx)
        return service

    def http_generate(self, service, **fields):
        connection = http.client.HTTPConnection('127.0.0.1', service.port, timeout=3)
        try:
            connection.request('POST', '/generate', body=json.dumps({'owner': 'test', **self.request, **fields}),
                               headers={'Authorization': 'Bearer ' + service.settings['service_token']})
            response = connection.getresponse()
            return response.status, [json.loads(line) for line in response.read().splitlines()]
        finally:
            connection.close()

    async def test_service_reuses_weights_between_conversations_and_unloads_when_idle(self):
        self.chunks = ['First', 'Second']
        service = self.service(idle_seconds=0.15)
        for owner in ('conversation-one', 'conversation-two'):
            status, events = await asyncio.to_thread(self.http_generate, service, owner=owner)
            self.assertEqual(status, 200)
            self.assertEqual(events[-1]['type'], 'finish')
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(self.active, 1)
        self.assertTrue(await asyncio.to_thread(service.closed.wait, 3))
        self.assertEqual(self.active, 0)
        self.assertFalse(service.mlx.lease.locked())

    async def test_switching_to_apple_unloads_mlx_before_starting_generation(self):
        async def apple(request, invoke):
            self.assertEqual(self.active, 0)
            yield {'type': 'text', 'delta': 'Apple answer'}
            yield {'type': 'finish', 'reason': 'stop'}
        self.namespace['apple_model_events'] = apple
        self.chunks = ['First', 'Second']
        service = self.service()
        await asyncio.to_thread(self.http_generate, service)
        self.assertEqual(self.active, 1)
        _, events = await asyncio.to_thread(self.http_generate, service, backend='apple_fm', model_id='')
        self.assertEqual(events[-1]['type'], 'finish')
        self.assertEqual(self.active, 0)
        await asyncio.to_thread(self.http_generate, service)
        self.assertEqual(len(self.loads), 2)
        self.assertEqual(self.peak_active, 1)
