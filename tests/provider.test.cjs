// Exercise the generated JS on the host with controlled fetch and model responses.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const { webcrypto } = require("node:crypto");

const project = path.resolve(__dirname, "..");
const python = process.env.TEST_PYTHON || "python3";
const script = execFileSync(python, ["-c", "from ai_setup.bundle import build_provider; from ai_setup.settings import defaults, provider_settings; print(build_provider(provider_settings(defaults())))"],
                            { cwd: project, encoding: "utf8" });
const schema = name => ({ name, description: name, inputSchema: { type: "object", properties: {} } });
const tools = ["run_python", "read_file", "write_file", "browser_open", "browser_read_text"].map(schema);

function runtime(fetch, groups = { files: false, python: false, browser: false }) {
  const sandbox = { fetch, AbortController, TextDecoder, TextEncoder, setTimeout, clearTimeout, crypto: webcrypto };
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox);
  const settings = JSON.parse(vm.runInContext("JSON.stringify(DEFAULT_SETTINGS)", sandbox));
  settings.groups = groups;
  sandbox.localStorage = { getItem: () => JSON.stringify(settings) };
  sandbox.build = vm.runInContext("BACKEND_BUILD", sandbox);
  return sandbox;
}

function context(records = [{ index: 0, type: "message", message: { role: "user", content: "你好" } }]) {
  return { apiVersion: 2, conversationId: "test", provider: { id: "test-provider" },
           instructions: "系统提示词 😀 保持原样", nativeTools: tools,
           preferences: { browserTools: false }, getTranscript: () => ({ records }) };
}

function health(sandbox) {
  return Response.json({ service: "pythona-local-llm", protocol: 2, build: sandbox.build });
}

function streamResponse(events, bytesPerChunk = 1) {
  const data = new TextEncoder().encode(events.map(event => JSON.stringify(event)).join("\n") + "\n");
  return new Response(new ReadableStream({ start(controller) {
    for (let i = 0; i < data.length; i += bytesPerChunk) controller.enqueue(data.slice(i, i + bytesPerChunk));
    controller.close();
  }}));
}

test("startup works with the Python group disabled and embeds standalone Python", async () => {
  const sandbox = runtime(async () => { throw new Error("offline"); });
  const events = [];
  await sandbox.stream(context(), event => events.push(event));
  assert.equal(events.length, 1);
  assert.equal(events[0].name, "run_python");
  assert.equal(events[0].metadata.apple_fm_service_start, true);
  execFileSync(python, ["-c", "import sys; compile(sys.stdin.read(), '<bootstrap>', 'exec')"], { input: events[0].input.code });
  assert.ok(!events[0].input.code.includes(project));
});

test("selected groups preserve instructions and decode split Unicode bytes", async () => {
  let body;
  const sandbox = runtime(async (url, options) => {
    if (url.endsWith("/health")) return health(sandbox);
    body = JSON.parse(options.body);
    return streamResponse([{ type: "text", delta: "你好 😀" }, { type: "finish", reason: "stop" }]);
  }, { files: true, browser: false, python: false });
  const events = [];
  await sandbox.stream(context(), event => events.push(event));
  assert.deepEqual(body.tools.map(tool => tool.name), ["read_file", "write_file"]);
  assert.equal(body.instructions, context().instructions);
  assert.equal(events[0].delta, "你好 😀");
});

test("browser filtering uses actual tools and excludes bootstrap history", async () => {
  let body;
  const sandbox = runtime(async (url, options) => {
    if (url.endsWith("/health")) return health(sandbox);
    body = JSON.parse(options.body);
    return streamResponse([{ type: "tool_request", run_id: "run", id: "call", name: "browser_open", input: { url: "https://example.test" } }]);
  }, { files: false, browser: true, python: false });
  const records = context().getTranscript().records;
  records.push({ index: 1, type: "message", message: { role: "assistant", parts: [], tool_calls: [
    { id: "boot", function: { name: "run_python", arguments: "BIG EMBEDDED CODE" }, metadata: { apple_fm_service_start: true } }
  ] } });
  records.push({ index: 2, type: "message", message: { role: "tool", tool_call_id: "boot", content: "started" } });
  records.push({ index: 3, type: "message", message: { role: "assistant", parts: [{ type: "text", text: "上一轮" }], tool_calls: [
    { id: "real", function: { name: "read_file", arguments: '{"path":"a.py"}' } }
  ] } });
  records.push({ index: 4, type: "message", message: { role: "tool", tool_call_id: "real", name: "read_file", content: "print(1)" } });
  const events = [];
  await sandbox.stream(context(records), event => events.push(event));
  assert.deepEqual(body.tools.map(tool => tool.name), ["browser_open", "browser_read_text"]);
  assert.equal(body.messages.length, 3);
  assert.ok(!JSON.stringify(body).includes("BIG EMBEDDED"));
  assert.equal(body.messages[1].content, "上一轮");
  assert.equal(body.messages[2].content, "print(1)");
  assert.equal(events[1].name, "browser_open");
});

test("failed startup does not loop and disabled tools are rejected", async () => {
  const sandbox = runtime(async () => { throw new Error("Unexpected network request"); });
  const records = context().getTranscript().records;
  records.push({ type: "message", message: { role: "assistant", tool_calls: [
    { id: "boot", metadata: { apple_fm_service_start: true } }
  ] } });
  records.push({ type: "message", message: { role: "tool", tool_call_id: "boot", tool_failed: true, content: "port busy" } });
  await assert.rejects(sandbox.stream(context(records), () => assert.fail()), /port busy/);
  const other = runtime(async url => url.endsWith("/health") ? health(other) : streamResponse([
    { type: "tool_request", run_id: "run", name: "write_file", id: "bad", input: {} }, { type: "finish", reason: "stop" }
  ]));
  await assert.rejects(other.stream(context(), () => assert.fail()), /disabled tool/);
});

test("truncated streams, model errors, and unrelated services fail", async () => {
  for (const events of [[{ type: "text", delta: "部分" }], [{ type: "error", message: "模型不可用" }]]) {
    const sandbox = runtime(async url => url.endsWith("/health") ? health(sandbox) : streamResponse(events));
    await assert.rejects(sandbox.stream(context(), () => {}), /without a terminal|不可用/);
  }
  const sandbox = runtime(async () => Response.json({ service: "unrelated" }));
  await assert.rejects(sandbox.stream(context(), () => assert.fail()), /not this project/);
});

function toolRound(events, content = "Permission denied", failed = true) {
  const records = context().getTranscript().records;
  const state = events.find(event => event.type === "provider_record");
  const call = events.find(event => event.type === "native_tool_call");
  records.push({ index: 1, ...JSON.parse(JSON.stringify(state)) });
  records.push({ index: 2, type: "message", message: { role: "assistant", parts: [], tool_calls: [
    { id: call.id, function: { name: call.name, arguments: JSON.stringify(call.input) } }
  ] } });
  records.push({ index: 3, type: "message", message: { role: "tool", name: call.name, tool_call_id: call.id,
    content, tool_failed: failed } });
  return records;
}

test("a new JS context resumes the same SDK run and passes native failure metadata", async () => {
  const first = runtime(async url => url.endsWith("/health") ? health(first) : streamResponse([
    { type: "tool_request", run_id: "same-session", id: "read", name: "read_file", input: { path: "a.py" } }
  ]), { files: true, browser: false, python: false });
  const events = [];
  await first.stream(context(), event => events.push(event));
  const records = toolRound(events);
  let request;
  const second = runtime(async (url, options) => {
    if (url.endsWith("/health")) return health(second);
    assert.ok(url.endsWith("/resume"));
    request = JSON.parse(options.body);
    return streamResponse([{ type: "text", delta: "Cannot read the file" }, { type: "finish", reason: "stop" }]);
  }, { files: true, browser: false, python: false });
  const output = [];
  await second.stream(context(records), event => output.push(event));
  assert.deepEqual(request, { owner: '["test-provider","test"]', run_id: "same-session",
    result: { id: "read", name: "read_file", content: "Permission denied", failed: true } });
  assert.equal(output.find(event => event.type === "provider_record").data, "null");
});

test("new user turns start fresh; lost SDK runs never replay tools or bootstrap", async () => {
  const first = runtime(async url => url.endsWith("/health") ? health(first) : streamResponse([
    { type: "tool_request", run_id: "lost", id: "call", name: "read_file", input: {} }
  ]), { files: true, browser: false, python: false });
  const events = [];
  await first.stream(context(), event => events.push(event));
  const records = toolRound(events);
  const offline = runtime(async () => { throw new Error("offline"); });
  await assert.rejects(offline.stream(context(records), () => assert.fail()), /service ended/);
  const alive = runtime(async url => url.endsWith("/health") ? health(alive) : new Response("expired", { status: 400 }),
    { files: true, browser: false, python: false });
  await assert.rejects(alive.stream(context(records), () => assert.fail()), /expired/);
  records.push({ index: 4, type: "message", message: { role: "user", content: "Try another request" } });
  const output = [];
  await offline.stream(context(records), event => output.push(event));
  assert.equal(output[0].metadata.apple_fm_service_start, true);
});

test("incomplete or malformed handoffs never execute native tools", async () => {
  for (const suffix of ["{", JSON.stringify({ type: "finish", reason: "stop" })]) {
    const sandbox = runtime(async url => url.endsWith("/health") ? health(sandbox) : new Response(
      JSON.stringify({ type: "tool_request", run_id: "run", id: "call", name: "read_file", input: {} }) + "\n" + suffix),
      { files: true, browser: false, python: false });
    await assert.rejects(sandbox.stream(context(), () => assert.fail("No tool or record should be committed")));
  }
});
