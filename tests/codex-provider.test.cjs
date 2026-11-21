// The repository-owned provider runs without an App template or Python service.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const script = execFileSync(process.env.TEST_PYTHON || 'python3', ['-c',
  'from ai_setup.bundle import build_provider; from ai_setup.settings import defaults, provider_settings; print(build_provider(provider_settings(defaults("codex"), {})))'],
  { cwd: root, encoding: 'utf8' });
const plain = value => JSON.parse(JSON.stringify(value));
const jwt = claims => 'x.' + Buffer.from(JSON.stringify(claims)).toString('base64url') + '.x';
const token = () => jwt({ exp: Date.now() / 1000 + 7200 });
function storage(settings = {}) {
  return new Map(Object.entries({ access_token: token(), refresh_token: 'refresh', account_id: 'account',
    codex_settings: JSON.stringify({ model_id: 'custom-model', reasoning_effort: 'high', ...settings }) }));
}
function runtime(fetch, values = storage()) {
  const sandbox = { fetch, TextDecoder, TextEncoder, atob, Uint8Array, AbortController, setTimeout, clearTimeout,
    localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } };
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox);
  return sandbox;
}
function context(records = [{ index: 0, type: 'message', message: { role: 'user', content: 'Hello' } }]) {
  return { apiVersion: 2, conversationId: 'same-conversation', provider: { id: 'provider' },
    instructions: '  Keep instructions unchanged. 中文 😀  ', preferences: { browserTools: false, webSearch: true },
    nativeTools: ['read_file', 'write_file', 'browser_open', 'run_python'].map(name =>
      ({ name, description: name, inputSchema: { type: 'object', properties: {} } })),
    getTranscript: () => ({ records }) };
}
function response(events) {
  const bytes = new TextEncoder().encode(events.map(event => 'data: ' + JSON.stringify(event) + '\n\n').join(''));
  return new Response(new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3));
    controller.close();
  }}));
}
const completed = output => ({ type: 'response.completed', response: { status: 'completed', output,
  usage: { input_tokens: 10, output_tokens: 5 } } });

test('installed defaults and standalone fallback agree on the current model', () => {
  const instance = runtime(() => assert.fail(), new Map());
  assert.equal(vm.runInContext('MODEL', instance), 'gpt-6.1-sol');
  assert.equal(vm.runInContext('FALLBACK_CODEX_SETTINGS.model_id', instance), 'gpt-6.1-sol');
});

test('project-owned model and reasoning use all native tools and preserve instructions and split Unicode', async () => {
  const calls = [];
  const instance = runtime(async (url, options) => {
    calls.push({ url, ...options });
    return response([{ type: 'response.output_text.delta', delta: 'Hello 中文 😀' }, completed([])]);
  });
  const events = [];
  await instance.stream(context(), event => events.push(plain(event)));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://chatgpt.com/backend-api/codex/responses');
  const body = JSON.parse(calls[0].body);
  assert.equal(body.model, 'custom-model');
  assert.deepEqual(body.reasoning, { effort: 'high', summary: 'auto' });
  assert.equal(body.instructions, context().instructions);
  assert.deepEqual(body.tools.map(tool => tool.name || tool.type), ['read_file', 'write_file', 'browser_open', 'run_python', 'web_search']);
  assert.equal(events.filter(event => event.type === 'text').map(event => event.delta).join(''), 'Hello 中文 😀');
  assert.equal(events.at(-1).type, 'finish');
  assert.equal(calls[0].headers['ChatGPT-Account-ID'], 'account');
  assert.equal(calls[0].headers.Version, '0.160.0');
  assert.equal(calls[0].headers['User-Agent'], 'codex_cli_rs/0.160.0 (iOS; arm64)');
  assert.equal(calls[0].headers['OpenAI-Beta'], 'responses=experimental');
  assert.ok(!script.includes('BACKEND_PAYLOAD'));
});

test('Codex forwards image-only prompts, tool images, and captioned photos in order', async () => {
  const png = 'data:image/png;base64,iVBORw0KGgo=';
  const jpeg = 'data:image/jpeg;base64,/9j/2Q==';
  const records = [
    { role: 'user', content: '', images: [jpeg] },
    { role: 'assistant', tool_calls: [{ id: 'chart', function: { name: 'view_image', arguments: '{"path":"chart.png"}' } }] },
    { role: 'tool', name: 'view_image', tool_call_id: 'chart', content: 'Image from chart.png', images: [png] },
    { role: 'assistant', parts: [{ type: 'text', text: 'The chart has two lines.' }] },
    { role: 'user', content: '  Compare these.  ', images: [png, jpeg] }
  ].map((message, index) => ({ type: 'message', index, message }));
  const ctx = context(records);
  ctx.preferences.imageUnderstanding = true;
  ctx.nativeTools.push({ name: 'view_image', inputSchema: { type: 'object', properties: { path: { type: 'string' } } } });
  let body;
  await runtime(async (_, options) => { body = JSON.parse(options.body); return response([completed([])]); })
    .stream(ctx, () => {});
  const pngBlock = { type: 'input_image', image_url: png };
  const jpegBlock = { type: 'input_image', image_url: jpeg };
  assert.ok(body.tools.some(tool => tool.type === 'function' && tool.name === 'view_image'));
  assert.deepEqual(body.input, [
    { role: 'user', content: [jpegBlock] },
    { type: 'function_call', call_id: 'chart', name: 'view_image', arguments: '{"path":"chart.png"}', status: 'completed' },
    { type: 'function_call_output', call_id: 'chart', output: [pngBlock, { type: 'input_text', text: 'Image from chart.png' }] },
    { role: 'assistant', content: 'The chart has two lines.' },
    { role: 'user', content: [pngBlock, jpegBlock, { type: 'input_text', text: '  Compare these.  ' }] }
  ]);
});

test('expired tokens rotate into provider storage and survive a fresh JS context', async () => {
  const values = storage();
  values.set('access_token', jwt({ exp: 1 }));
  const requests = [];
  const fetch = async (url, options) => {
    requests.push(url);
    if (url.endsWith('/oauth/token')) {
      assert.equal(JSON.parse(options.body).refresh_token, 'refresh');
      return Response.json({ access_token: token(), refresh_token: 'renewed',
        id_token: jwt({ email: 'new@example.test', 'https://api.openai.com/auth': { chatgpt_account_id: 'new-account' } }) });
    }
    assert.equal(options.headers['ChatGPT-Account-ID'], 'new-account');
    return response([completed([])]);
  };
  await runtime(fetch, values).stream(context(), () => {});
  await runtime(fetch, values).stream(context(), () => {});
  assert.equal(values.get('refresh_token'), 'renewed');
  assert.equal(values.get('email'), 'new@example.test');
  assert.equal(requests.filter(url => url.endsWith('/oauth/token')).length, 1);
});

test('401 retries once with renewed credentials and missing login reports the setup action', async () => {
  let inference = 0, refresh = 0;
  const instance = runtime(async url => {
    if (url.endsWith('/oauth/token')) { refresh++; return Response.json({ access_token: token(), refresh_token: 'renewed' }); }
    return ++inference === 1 ? new Response('', { status: 401 }) : response([completed([])]);
  });
  await instance.stream(context(), () => {});
  assert.equal(inference, 2);
  assert.equal(refresh, 1);
  const missing = storage();
  missing.delete('refresh_token');
  await assert.rejects(runtime(() => assert.fail(), missing).stream(context(), () => {}), /Sign in.*Pythona AI Setup/);
});

test('native tool handoff preserves encrypted reasoning across stateless invocations without replay', async () => {
  const output = [{ type: 'reasoning', id: 'reason', encrypted_content: 'opaque-encrypted', summary: [] },
    { type: 'function_call', id: 'output-call', call_id: 'read-call', name: 'read_file', arguments: '{"path":"a.py"}' }];
  const events = [];
  await runtime(async () => response([completed(output)])).stream(context(), event => events.push(plain(event)));
  const call = events.find(event => event.type === 'native_tool_call_delta' && event.name);
  assert.equal(call.name, 'read_file');
  const checkpoint = events.find(event => event.type === 'provider_record');
  assert.ok(checkpoint.data.includes('opaque-encrypted'));
  const records = [...context().getTranscript().records,
    { index: 1, type: 'message', message: { role: 'assistant', parts: [], tool_calls: [
      { id: 'read-call', function: { name: 'read_file', arguments: '{"path":"a.py"}' } } ] } },
    { index: 2, ...checkpoint },
    { index: 3, type: 'message', message: { role: 'tool', tool_call_id: 'read-call', name: 'read_file', content: 'print(1)' } }];
  let body;
  const nextEvents = [];
  await runtime(async (_, options) => { body = JSON.parse(options.body); return response([completed([])]); })
    .stream(context(records), event => nextEvents.push(plain(event)));
  assert.equal(body.input.filter(item => item.type === 'function_call').length, 1);
  assert.deepEqual(body.input.filter(item => item.type === 'function_call_output'), [
    { type: 'function_call_output', call_id: 'read-call', output: 'print(1)' }]);
  assert.ok(body.input.some(item => item.encrypted_content === 'opaque-encrypted'));
  assert.ok(!nextEvents.some(event => event.type.startsWith('native_tool')));
});

test('unknown tools, invalid arguments and truncated streams never execute a tool', async () => {
  for (const [name, args, pattern] of [['unknown_tool', '{}', /unknown tool/], ['read_file', '[]', /invalid tool arguments/]]) {
    const instance = runtime(async () => response([completed([
      { type: 'function_call', call_id: 'bad', name, arguments: args } ])]));
    const events = [];
    await assert.rejects(instance.stream(context(), event => events.push(event)), pattern);
    assert.ok(!events.some(event => event.type === 'native_tool_call_delta' && event.name));
  }
  await assert.rejects(runtime(async () => response([{ type: 'response.output_text.delta', delta: 'Partial' }]))
    .stream(context(), () => {}), /before a terminal/);
});

test('a competing refresh failure reuses saved credentials without overwriting them', async () => {
  const values = storage();
  values.set('access_token', jwt({ exp: 1 }));
  const pending = [];
  const fresh = token();
  const fetch = async (url, options) => {
    if (url.endsWith('/oauth/token')) return new Promise(resolve => pending.push(resolve));
    assert.equal(options.headers.Authorization, 'Bearer ' + fresh);
    return response([completed([])]);
  };
  const first = runtime(fetch, values).stream(context(), () => {});
  const second = runtime(fetch, values).stream(context(), () => {});
  assert.equal(pending.length, 2);
  pending[0](Response.json({ access_token: fresh, refresh_token: 'renewed' }));
  await first;
  pending[1](Response.json({ error: 'invalid_grant' }, { status: 400 }));
  await second;
  assert.equal(values.get('refresh_token'), 'renewed');
});

test('a late Codex refresh respects both newer credentials and account replacement', async () => {
  for (const changedAccount of [false, true]) {
    const values = storage();
    values.set('access_token', jwt({ exp: 1 }));
    const running = runtime(async url => {
      if (url.endsWith('/oauth/token')) {
        values.set('access_token', token());
        values.set('refresh_token', 'newer');
        if (changedAccount) values.set('account_id', 'other-account');
        return Response.json({ access_token: 'stale', refresh_token: 'stale' });
      }
      return response([completed([])]);
    }, values).stream(context(), () => {});
    if (changedAccount) await assert.rejects(running, /account changed/);
    else await running;
    assert.equal(values.get('refresh_token'), 'newer');
  }
});

test('Codex displays streamed and terminal refusal text without duplicating it', async () => {
  const text = 'I cannot help with that request.';
  const output = [{ type: 'message', content: [{ type: 'refusal', refusal: text }] }];
  for (const streamed of [false, true]) {
    const events = [];
    await runtime(async () => response([
      ...(streamed ? [{ type: 'response.refusal.delta', output_index: 0, content_index: 0, delta: text }] : []),
      completed(output)
    ])).stream(context(), event => events.push(plain(event)));
    assert.equal(events.filter(event => event.type === 'text').map(event => event.delta).join(''), text);
    assert.equal(events.at(-1).type, 'finish');
  }
});
