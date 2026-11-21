// Exercise the installed public ChatGPT provider across fresh JS contexts.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const script = execFileSync(process.env.TEST_PYTHON || 'python3', ['-c',
  'from ai_setup.bundle import build_provider; from ai_setup.settings import defaults, provider_settings; print(build_provider(provider_settings(defaults("chatgpt"), {})))'],
  { cwd: root, encoding: 'utf8' });
const plain = value => JSON.parse(JSON.stringify(value));
const auth = () => ({ client_id: 'oaiapp_one', subject: 'user', issuer: 'https://auth.openai.com', local_grant_id: 'login-one',
  access_token: 'chatgpt-access', refresh_token: 'chatgpt-refresh', id_token: 'verified-identity',
  scope: 'openid offline_access chatgpt.tokens.use.direct', expires_at: String(Date.now() / 1000 + 3600) });
function storage(account = auth()) {
  return new Map(Object.entries({ chatgpt_auth: JSON.stringify(account), access_token: 'old-codex-access',
    refresh_token: 'old-codex-refresh', codex_settings: JSON.stringify({ model_id: 'wrong-model', reasoning_effort: 'none' }),
    chatgpt_settings: JSON.stringify({ model_id: 'account-model', reasoning_effort: 'high' }) }));
}
function runtime(fetch, values = storage()) {
  const sandbox = { fetch, TextDecoder, TextEncoder, atob, Uint8Array, AbortController, setTimeout, clearTimeout,
    localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } };
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox);
  return sandbox;
}
function context(records = [{ index: 0, type: 'message', message: { role: 'user', content: 'Hello' } }]) {
  return { apiVersion: 2, conversationId: 'conversation', provider: { id: 'provider' },
    instructions: '  Preserve all instructions. 中文 😀  ', preferences: { browserTools: false, webSearch: true, imageUnderstanding: true },
    nativeTools: ['read_file', 'write_file', 'browser_open', 'run_python', 'view_image'].map(name =>
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
  usage: { input_tokens: 12, output_tokens: 6 } } });

test('ChatGPT uses its own account, public endpoint, and namespaced native tools', async () => {
  const calls = [], events = [];
  await runtime(async (url, options) => {
    calls.push({ url, ...options });
    return response([{ type: 'response.output_text.delta', delta: 'Hello 中文 😀' }, completed([])]);
  }).stream(context(), event => events.push(plain(event)));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://api.openai.com/v1/responses');
  const headers = calls[0].headers;
  assert.equal(headers.Authorization, 'Bearer chatgpt-access');
  assert.equal(headers['User-Agent'], 'PythonaAISetup/1.0 (iOS)');
  for (const key of ['originator', 'ChatGPT-Account-ID', 'Version', 'OpenAI-Beta']) assert.ok(!(key in headers));
  const body = JSON.parse(calls[0].body);
  assert.equal(body.model, 'account-model');
  assert.equal(body.instructions, context().instructions);
  assert.equal(body.store, false);
  assert.equal(body.stream, true);
  assert.equal(body.tools[0].type, 'namespace');
  assert.equal(body.tools[0].name, 'pythona');
  assert.deepEqual(body.tools[0].tools.map(tool => tool.name), context().nativeTools.map(tool => tool.name));
  assert.equal(body.tools[1].type, 'web_search');
  assert.ok(!('previous_response_id' in body));
  assert.equal(events.filter(event => event.type === 'text').map(event => event.delta).join(''), 'Hello 中文 😀');
  assert.equal(events.at(-1).type, 'finish');
});

test('replacement refresh tokens are saved together and survive fresh contexts', async () => {
  const values = storage({ ...auth(), expires_at: '0' });
  let refreshes = 0;
  const fetch = async (url, options) => {
    if (url.endsWith('/oauth/token')) {
      refreshes++;
      assert.equal(url, 'https://auth.openai.com/api/accounts/oauth/token');
      const form = new URLSearchParams(options.body);
      assert.equal(form.get('client_id'), 'oaiapp_one');
      assert.equal(form.get('refresh_token'), 'chatgpt-refresh');
      assert.equal(form.get('resource'), 'https://api.openai.com/v1');
      assert.equal(form.has('scope'), false);
      return Response.json({ access_token: 'new-access', refresh_token: 'new-refresh', expires_in: 3600,
        id_token: 'unverified-token' });
    }
    assert.equal(options.headers.Authorization, 'Bearer new-access');
    return response([completed([])]);
  };
  await runtime(fetch, values).stream(context(), () => {});
  await runtime(fetch, values).stream(context(), () => {});
  assert.equal(refreshes, 1);
  const saved = JSON.parse(values.get('chatgpt_auth'));
  assert.equal(saved.refresh_token, 'new-refresh');
  assert.equal(saved.id_token, 'verified-identity');
  assert.equal(saved.subject, 'user');
  assert.equal(values.get('refresh_token'), 'old-codex-refresh');
});

test('identity-only grants stop inference and a 401 only retries once', async () => {
  const broken = storage();
  broken.set('chatgpt_auth', 'secret-corrupted-credential');
  await assert.rejects(runtime(() => assert.fail(), broken).stream(context(), () => {}), error =>
    /Sign in to ChatGPT/.test(error.message) && !error.message.includes('secret-corrupted'));
  await assert.rejects(runtime(async () => new Response('secret-invalid-token-response'), storage({ ...auth(), expires_at: '0' }))
    .stream(context(), () => {}), error => /invalid token response/.test(error.message) && !error.message.includes('secret-invalid'));
  await assert.rejects(runtime(() => assert.fail(), storage({ ...auth(), scope: 'openid email' }))
    .stream(context(), () => {}), /Allow ChatGPT plan usage/);
  let inference = 0, refresh = 0;
  const instance = runtime(async url => {
    if (url.endsWith('/oauth/token')) {
      refresh++; return Response.json({ access_token: 'new', refresh_token: 'rotated', expires_in: 3600 });
    }
    inference++; return new Response('', { status: 401 });
  });
  await assert.rejects(instance.stream(context(), () => {}), /HTTP 401/);
  assert.equal(inference, 2);
  assert.equal(refresh, 1);
});

test('view_image round trips retain the namespace, reasoning, and images from the current transcript', async () => {
  const photo = 'data:image/jpeg;base64,/9j/2Q==';
  const plot = 'data:image/png;base64,iVBORw0KGgo=';
  const user = { index: 0, type: 'message', message: { role: 'user', content: 'Compare with plot.png', images: [photo] } };
  const output = [{ type: 'reasoning', id: 'reason', encrypted_content: 'opaque', summary: [] },
    { type: 'function_call', namespace: 'pythona', call_id: 'plot-call', name: 'view_image', arguments: '{"path":"plot.png"}' }];
  const events = [];
  await runtime(async () => response([completed(output)])).stream(context([user]), event => events.push(plain(event)));
  assert.equal(events.find(event => event.type === 'native_tool_call_delta' && event.name).name, 'view_image');
  const checkpoint = events.find(event => event.type === 'provider_record');
  assert.match(checkpoint.namespace, /^chatgpt:oaiapp_one:/);
  assert.ok(!checkpoint.data.includes(photo));
  const records = [user,
    { index: 1, type: 'message', message: { role: 'assistant', parts: [], tool_calls: [
      { id: 'plot-call', function: { name: 'view_image', arguments: '{"path":"plot.png"}' } } ] } },
    { index: 2, ...checkpoint },
    { index: 3, type: 'message', message: { role: 'tool', tool_call_id: 'plot-call', name: 'view_image', content: 'Image from plot.png', images: [plot] } }];
  let body;
  const next = [];
  await runtime(async (_, options) => { body = JSON.parse(options.body); return response([completed([])]); })
    .stream(context(records), event => next.push(plain(event)));
  assert.deepEqual(body.input, [
    { role: 'user', content: [{ type: 'input_image', image_url: photo }, { type: 'input_text', text: user.message.content }] },
    ...output,
    { type: 'function_call_output', call_id: 'plot-call', output: [
      { type: 'input_image', image_url: plot }, { type: 'input_text', text: 'Image from plot.png' }] }
  ]);
  assert.ok(!next.some(event => event.type.startsWith('native_tool')));
  const omitted = 'Image omitted by Pythona.';
  const disabled = context(records.map(record => record.message?.images
    ? { ...record, message: { ...record.message, images: [], content: record.message.content + '\n' + omitted } }
    : record));
  disabled.preferences.imageUnderstanding = false;
  disabled.nativeTools = disabled.nativeTools.filter(tool => tool.name !== 'view_image');
  await runtime(async (_, options) => {
    const request = JSON.parse(options.body);
    assert.ok(!request.tools[0].tools.some(tool => tool.name === 'view_image'));
    assert.ok(!options.body.includes('data:image/'));
    assert.equal(request.input.at(-1).output, 'Image from plot.png\n' + omitted);
    return response([completed([])]);
  }).stream(disabled, () => {});
  await runtime(async (_, options) => {
    assert.ok(!JSON.parse(options.body).input.some(item => item.encrypted_content === 'opaque'));
    return response([completed([])]);
  }, storage({ ...auth(), client_id: 'oaiapp_another' })).stream(context(records), () => {});
});

test('quota failures and incomplete streams never hand off a tool', async () => {
  const call = { type: 'function_call', namespace: 'pythona', call_id: 'call', name: 'run_python', arguments: '{}' };
  for (const terminal of [{ type: 'response.failed', response: { error: { code: 'subscription_sharing_usage_limit_exceeded' } } },
    { type: 'response.incomplete', response: { output: [call] } }]) {
    const events = [];
    await assert.rejects(runtime(async () => response([{ type: 'response.output_item.done', item: call }, terminal]))
      .stream(context(), event => events.push(plain(event))), /usage limit|did not complete/);
    assert.ok(!events.some(event => event.type === 'native_tool_call_delta' && event.name));
    assert.ok(!events.some(event => event.type === 'finish'));
  }
  await assert.rejects(runtime(async () => response([completed([{ ...call, namespace: 'untrusted' }])]))
    .stream(context(), () => {}), /unknown tool namespace/);
  await assert.rejects(runtime(async () => response([
    { ...call, type: 'response.function_call_arguments.done', namespace: 'untrusted' }, completed([])]))
    .stream(context(), () => {}), /unknown tool namespace/);
  await assert.rejects(runtime(async () => response([completed([{ ...call, call_id: '' }])]))
    .stream(context(), () => {}), /without a call ID/);
});

test('a concurrent account change is not overwritten by an old refresh response', async () => {
  const values = storage({ ...auth(), expires_at: '0' });
  await assert.rejects(runtime(async () => {
    values.set('chatgpt_auth', JSON.stringify({ ...auth(), client_id: 'oaiapp_other', refresh_token: 'newer' }));
    return Response.json({ access_token: 'stale', refresh_token: 'stale', expires_in: 3600 });
  }, values).stream(context(), () => {}), /account changed/);
  assert.equal(JSON.parse(values.get('chatgpt_auth')).refresh_token, 'newer');
});

test('confirmed expired sessions clear tokens but retain registration; network failures preserve both', async () => {
  for (const [status, body, cleared] of [[400, { error: 'invalid_grant' }, true], [503, { error: 'temporarily_unavailable' }, false]]) {
    const values = storage({ ...auth(), expires_at: '0' });
    await assert.rejects(runtime(async () => Response.json(body, { status }), values).stream(context(), () => {}), /ChatGPT/);
    const saved = JSON.parse(values.get('chatgpt_auth'));
    assert.equal(saved.client_id, 'oaiapp_one');
    assert.equal(saved.subject, 'user');
    assert.equal('refresh_token' in saved, !cleared);
    assert.equal('id_token' in saved, !cleared);
  }
});

test('concurrent refresh success survives either response order and is reused after failure', async () => {
  for (const failureFirst of [false, true]) {
    const values = storage({ ...auth(), expires_at: '0' });
    const pending = [];
    const fetch = async (url, options) => {
      if (url.endsWith('/oauth/token')) return new Promise(resolve => pending.push(resolve));
      assert.equal(options.headers.Authorization, 'Bearer renewed-access');
      return response([completed([])]);
    };
    const first = runtime(fetch, values).stream(context(), () => {}).then(() => null, error => error);
    const second = runtime(fetch, values).stream(context(), () => {}).then(() => null, error => error);
    assert.equal(pending.length, 2);
    const success = () => Response.json({ access_token: 'renewed-access', refresh_token: 'renewed-refresh', expires_in: 3600 });
    const failure = () => Response.json({ error: 'invalid_grant' }, { status: 400 });
    pending[0](failureFirst ? failure() : success());
    const firstError = await first;
    assert.equal(Boolean(firstError), failureFirst);
    pending[1](failureFirst ? success() : failure());
    assert.equal(await second, null);
    const saved = JSON.parse(values.get('chatgpt_auth'));
    assert.equal(saved.refresh_token, 'renewed-refresh');
    assert.equal(saved.id_token, 'verified-identity');
    await runtime(fetch, values).stream(context(), () => {});
    assert.equal(pending.length, 2);
  }
});

test('a late successful refresh cannot replace a newer token, a new login, or a deleted account', async () => {
  for (const change of ['rotated', 'relogin', 'deleted']) {
    const values = storage({ ...auth(), expires_at: '0' });
    let complete;
    const fetch = async url => {
      if (url.endsWith('/oauth/token')) return new Promise(resolve => { complete = resolve; });
      return response([completed([])]);
    };
    const running = runtime(fetch, values).stream(context(), () => {}).then(() => null, error => error);
    if (change === 'deleted') values.delete('chatgpt_auth');
    else values.set('chatgpt_auth', JSON.stringify({ ...auth(), access_token: 'newer', refresh_token: 'newer',
      local_grant_id: change === 'relogin' ? 'login-two' : 'login-one' }));
    complete(Response.json({ access_token: 'stale', refresh_token: 'stale', expires_in: 3600 }));
    assert.equal(Boolean(await running), change !== 'rotated');
    if (change === 'deleted') assert.equal(values.has('chatgpt_auth'), false);
    else assert.equal(JSON.parse(values.get('chatgpt_auth')).refresh_token, 'newer');
  }
});

test('refusal deltas and terminal refusals display once and remain in the checkpoint', async () => {
  const text = 'I cannot help with that request.';
  const output = [{ type: 'message', id: 'refusal', role: 'assistant', content: [{ type: 'refusal', refusal: text }] }];
  for (const streamed of [false, true]) {
    const events = [];
    const input = streamed ? [
      { type: 'response.refusal.delta', output_index: 0, content_index: 0, delta: 'I cannot ' },
      { type: 'response.refusal.delta', output_index: 0, content_index: 0, delta: 'help with that request.' },
      { type: 'response.refusal.done', output_index: 0, content_index: 0, refusal: text }
    ] : [];
    await runtime(async () => response([...input, completed(output)])).stream(context(), event => events.push(plain(event)));
    assert.equal(events.filter(event => event.type === 'text').map(event => event.delta).join(''), text);
    assert.ok(events.find(event => event.type === 'provider_record').data.includes(text));
    assert.equal(events.at(-1).type, 'finish');
  }
});
