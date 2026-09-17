// DEFAULT_SETTINGS, BACKEND_PAYLOAD, and BACKEND_BUILD are injected by bundle.py.
const SETTINGS_KEY = "local_model_settings";
const RUN_NAMESPACE = "local_model_tool_run";
const BOOTSTRAP_TAG = "local_model_service_start";
const FILE_TOOLS = new Set(["read_file", "write_file", "edit_file", "list_files", "grep", "glob"]);

function settingsForRun() {
  const saved = localStorage.getItem(SETTINGS_KEY);
  const settings = saved === null ? DEFAULT_SETTINGS : JSON.parse(saved);
  if (!settings || !Number.isInteger(settings.port) || settings.port < 1024 || settings.port > 65535
      || typeof settings.service_token !== "string" || settings.service_token.length < 16
      || !["apple_fm", "mlx_lm"].includes(settings.backend)
      || typeof settings.model_id !== "string" || (settings.backend === "mlx_lm" && !settings.model_id.trim())
      || !Number.isInteger(settings.maximum_response_tokens) || settings.maximum_response_tokens < 1 || settings.maximum_response_tokens > 8192
      || !settings.groups || ["files", "browser", "python"].some(k => typeof settings.groups[k] !== "boolean")) {
    throw new Error("Invalid local model settings. Run setup again.");
  }
  return settings;
}

function selectedTools(ctx, settings) {
  return ctx.nativeTools.filter(tool =>
    (settings.groups.files && FILE_TOOLS.has(tool.name))
    || (settings.groups.browser && tool.name.startsWith("browser_"))
    || (settings.groups.python && tool.name === "run_python"));
}

function transcriptMessages(records) {
  const internalCalls = new Set();
  for (const record of records) {
    if (record.type !== "message") continue;
    for (const call of record.message.tool_calls || []) {
      if (call.metadata && call.metadata[BOOTSTRAP_TAG]) internalCalls.add(call.id);
    }
  }
  const messages = [];
  for (const record of records) {
    if (record.type !== "message") continue;
    const message = record.message;
    if (message.role === "tool" && internalCalls.has(message.tool_call_id)) continue;
    if (message.role === "assistant") {
      const text = (message.parts || []).filter(part => part.type === "text").map(part => part.text).join("");
      const calls = (message.tool_calls || []).filter(call => !internalCalls.has(call.id));
      if (text || calls.length) {
        const entry = { role: "assistant", content: text };
        if (calls.length) entry.tool_calls = calls.map(call => ({ id: call.id, name: call.function.name,
                                                               arguments: call.function.arguments }));
        messages.push(entry);
      }
    } else if (message.role === "user" || message.role === "tool") {
      const entry = { role: message.role, content: message.content || "" };
      if (message.role === "tool") {
        entry.name = message.name;
        entry.tool_call_id = message.tool_call_id;
        entry.failed = message.tool_failed === true;
      }
      messages.push(entry);
    }
  }
  return messages;
}

function bootstrapResultThisTurn(records) {
  let calls = new Set();
  let result = null;
  for (const record of records) {
    if (record.type !== "message") continue;
    const message = record.message;
    if (message.role === "user") { calls = new Set(); result = null; }
    for (const call of message.tool_calls || []) {
      if (call.metadata && call.metadata[BOOTSTRAP_TAG]) calls.add(call.id);
    }
    if (message.role === "tool" && calls.has(message.tool_call_id)) result = message;
  }
  return result;
}

async function serviceHealth(base, settings) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2000);
  try {
    let response;
    try {
      response = await fetch(base + "/health", { signal: controller.signal,
        headers: { Authorization: "Bearer " + settings.service_token } });
    } catch (_) { return null; }
    if (!response.ok) throw new Error("The local endpoint returned HTTP " + response.status + ". Check the service configuration.");
    const health = await response.json();
    if (health.service !== "pythona-local-llm" || health.protocol !== 3) {
      throw new Error("The endpoint is not this project's model service.");
    }
    return health;
  } finally { clearTimeout(timer); }
}

function startCode(settings) {
  // Double JSON encoding produces a Python string literal, not executable configuration code.
  return "import base64 as _b, zlib as _z, json as _j\n"
    + "_scope = {'__name__': '_local_model_provider_backend'}\n"
    + "exec(compile(_z.decompress(_b.b64decode(" + JSON.stringify(BACKEND_PAYLOAD)
    + ")), '<pythona>', 'exec'), _scope)\n"
    + "_config = _j.loads(" + JSON.stringify(JSON.stringify(settings)) + ")\n"
    + "print(_j.dumps(_scope['start_service'](_config, " + JSON.stringify(BACKEND_BUILD)
    + "), ensure_ascii=False))\n";
}

function pendingToolRun(records) {
  let pending = null;
  let messages = [];
  for (const record of records) {
    if (record.type === "message") {
      if (record.message.role === "user") { pending = null; messages = []; }
      messages.push(record.message);
    } else if (record.type === "provider_record" && record.namespace === RUN_NAMESPACE) {
      pending = JSON.parse(record.data);
    }
  }
  if (pending === null) return null;
  if (!pending || pending.build !== BACKEND_BUILD
      || [pending.run_id, pending.id, pending.name].some(value => typeof value !== "string" || !value)) {
    throw new Error("The saved model request is invalid; send a new message to continue.");
  }
  const results = messages.filter(message => message.role === "tool" && message.tool_call_id === pending.id);
  if (results.length !== 1 || results[0].name !== pending.name || typeof results[0].content !== "string") {
    throw new Error("The pending model tool has no matching result; send a new message to continue.");
  }
  return { run_id: pending.run_id, result: { id: pending.id, name: pending.name,
    content: results[0].content, failed: results[0].tool_failed === true } };
}

async function readEvents(response, emit, allowed) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let pending = "", terminal = null, total = 0;
  function line(text) {
    if (!text.trim()) return;
    if (terminal) throw new Error("The model service continued after a terminal event.");
    const event = JSON.parse(text);
    if (event.type === "error") throw new Error(event.message || "The model service failed.");
    if (event.type === "text") {
      if (typeof event.delta !== "string") throw new Error("Invalid text event.");
      emit({ type: "text", delta: event.delta });
    } else if (event.type === "tool_request") {
      if (!allowed.has(event.name) || typeof event.id !== "string" || !event.id
          || typeof event.run_id !== "string" || !event.run_id
          || !event.input || typeof event.input !== "object" || Array.isArray(event.input)) {
        throw new Error("The model returned a disabled tool or invalid arguments.");
      }
      terminal = event;
    } else if (event.type === "finish") {
      if (typeof event.reason !== "string") throw new Error("Invalid finish event.");
      terminal = event;
    } else { throw new Error("Unknown model event: " + event.type); }
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > 4 * 1024 * 1024) throw new Error("Model output exceeds the limit.");
      pending += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = pending.indexOf("\n")) !== -1) {
        line(pending.slice(0, newline));
        pending = pending.slice(newline + 1);
      }
    }
    pending += decoder.decode();
    line(pending);
    if (!terminal) throw new Error("The connection ended without a terminal event.");
    // Commit tool calls only after receiving the complete handoff response.
    return terminal;
  } finally {
    try { await reader.cancel(); } catch (_) {}
  }
}

async function stream(ctx, emit) {
  if (ctx.apiVersion < 2) throw new Error("Custom Provider API v2 is required.");
  const settings = settingsForRun();
  const records = ctx.getTranscript().records;
  const pending = pendingToolRun(records);
  const startup = bootstrapResultThisTurn(records);
  if (startup && startup.tool_failed) throw new Error("Local model service startup failed:\n" + startup.content);
  const base = "http://127.0.0.1:" + settings.port;
  const health = await serviceHealth(base, settings);
  if (!health || health.build !== BACKEND_BUILD) {
    if (pending) throw new Error("The model service ended while awaiting a tool; send a new message to continue.");
    if (startup) throw new Error("The service is still unavailable after startup. Check the tool result and retry.");
    if (!ctx.nativeTools.some(tool => tool.name === "run_python")) {
      throw new Error("Starting the local service requires the App's run_python tool.");
    }
    emit({ type: "native_tool_call", id: "localmodel_start_" + crypto.randomUUID(),
      name: "run_python", displayName: "Start local model service", metadata: { [BOOTSTRAP_TAG]: true },
      input: { code: startCode(settings) } });
    return;
  }
  const tools = selectedTools(ctx, settings);
  const allowed = new Set(tools.map(tool => tool.name));
  if (pending && !allowed.has(pending.result.name)) throw new Error("The pending tool is no longer enabled.");
  const owner = JSON.stringify([ctx.provider.id, ctx.conversationId]);
  const body = pending ? { owner, ...pending }
    : { owner, backend: settings.backend, model_id: settings.model_id,
        instructions: ctx.instructions, messages: transcriptMessages(records), tools,
        maximum_response_tokens: settings.maximum_response_tokens };
  const response = await fetch(base + (pending ? "/resume" : "/generate"), {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + settings.service_token },
    body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error("Model service HTTP " + response.status + ": " + await response.text());
  const terminal = await readEvents(response, emit, allowed);
  if (terminal.type === "tool_request") {
    if (pending && (terminal.run_id !== pending.run_id || terminal.id === pending.result.id)) {
      throw new Error("The model service returned a mismatched or duplicate tool call.");
    }
    emit({ type: "provider_record", namespace: RUN_NAMESPACE,
      data: JSON.stringify({ build: BACKEND_BUILD, run_id: terminal.run_id, id: terminal.id, name: terminal.name }) });
    emit({ type: "native_tool_call", id: terminal.id, name: terminal.name, input: terminal.input });
  } else {
    emit({ type: "provider_record", namespace: RUN_NAMESPACE, data: "null" });
    emit({ type: "finish", reason: terminal.reason });
  }
}
