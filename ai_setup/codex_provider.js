// Codex provider maintained by Pythona AI Setup.
// Initially adapted from Pythona's Codex demo; this repository owns this copy.
//
// Usage:
// 1. Install through Pythona AI Setup, or copy this file into a Custom provider.
// 2. Put these fields into this provider's localStorage.
//
//    {
//      "refresh_token": "...",
//      "access_token": "...",
//      "id_token": "...",
//      "account_id": "..."
//    }
//
// 3. Save the connection and chat.
//
// The script owns OAuth refresh, the Codex request, SSE parsing, hosted tool
// search events, citations, usage and tool-call mapping.
//
// Thinking / reasoning options:
// - REASONING_EFFORT: null, "none", "minimal", "low", "medium", "high", "xhigh", "max"
// - REASONING_SUMMARY: null, "auto", "concise", "detailed"
// Pythona displays returned reasoning summaries/text as reasoning.
// Complete Responses output arrays are persisted as provider_record checkpoints
// so encrypted reasoning and assistant phase survive the next stateless request.
// The stable ctx.conversationId is used as the prompt cache routing key.

// Configuration

const CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann";
const CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token";
const CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses";
const CODEX_VERSION = "0.160.0";
const CODEX_USER_AGENT = "codex_cli_rs/" + CODEX_VERSION + " (iOS; arm64)";
const CHATGPT_ENABLED = typeof CHATGPT_PROVIDER !== "undefined";
const CODEX_SETTINGS_KEY = CHATGPT_ENABLED ? CHATGPT_PROVIDER.settingsKey : "codex_settings";
const FALLBACK_CODEX_SETTINGS = { model_id: "gpt-6.1-sol", reasoning_effort: "medium" };
const CODEX_SETTINGS = loadCodexSettings();
const MODEL = CODEX_SETTINGS.model_id;

function loadCodexSettings() {
  const saved = localStorage.getItem(CODEX_SETTINGS_KEY);
  const value = saved === null
    ? (typeof DEFAULT_CODEX_SETTINGS === "undefined" ? FALLBACK_CODEX_SETTINGS : DEFAULT_CODEX_SETTINGS)
    : JSON.parse(saved);
  if (!value || typeof value.model_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.model_id)
      || !["none", "minimal", "low", "medium", "high", "xhigh", "max"].includes(value.reasoning_effort)) {
    throw new Error("Invalid provider settings. Open Pythona AI Setup and save this provider again.");
  }
  return value;
}

const REASONING_EFFORT = CODEX_SETTINGS.reasoning_effort; // bare null | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"
const REASONING_SUMMARY = "auto"; // bare null | "auto" | "concise" | "detailed"
// Provider-owned provider_record checkpoint format, not a Codex/OpenAI API field.
const CHECKPOINT_FORMAT = "codex-responses-checkpoint-v2";
const CITATION_START = "\uE200";
const CITATION_DELIMITER = "\uE202";
const CITATION_STOP = "\uE201";
const CITATION_PREFIX = CITATION_START + "cite" + CITATION_DELIMITER;
const CITATION_SOURCE_ID_RE = /^[A-Za-z0-9_-]+$/;
const CITATION_LINE_LOCATOR_RE = /^L\d+(?:-L\d+)?$/;
const CITATION_MAX_CANDIDATE_LENGTH = 4096; // JavaScript string lengths are UTF-16 code units.

// Entry

async function stream(ctx, emit) {
  if (ctx.apiVersion < 2) throw new Error("Custom Provider API v2 is required.");
  try {
    await runCodexRequest(ctx, emit);
  } catch (error) {
    throw new Error(providerErrorMessage(error));
  }
}

async function runCodexRequest(ctx, emit) {
  const request = buildCodexRequest(ctx);
  let auth = await freshAuth();
  let response = await fetchCodexResponse(auth, request.body);

  if (response.status === 401) {
    auth = await refreshAuth(auth);
    response = await fetchCodexResponse(auth, request.body);
  }

  if (!response.ok) {
    if (CHATGPT_ENABLED) throw new Error(await CHATGPT_PROVIDER.httpError(response));
    throw new Error("Codex request failed (HTTP " + response.status + "). " + await response.text());
  }

  await consumeResponseStream(response, emit, request);
}

function providerErrorMessage(error) {
  if (error == null) return "Unknown error.";
  if (typeof error === "string") return error || "Unknown error.";
  if (error.message) return String(error.message);
  if (error.response) {
    return error.name ? String(error.name) + ": " + String(error.response) : String(error.response);
  }
  if (error.name) return String(error.name);

  try {
    const json = JSON.stringify(error);
    if (json && json !== "{}") return json;
  } catch (_) {}

  const text = String(error);
  return text && text !== "[object Object]" ? text : "Unknown error.";
}

// Auth

async function freshAuth() {
  if (CHATGPT_ENABLED) return await CHATGPT_PROVIDER.freshAuth();
  const auth = loadAuth();
  if (shouldRefresh(auth)) return await refreshAuth(auth);
  return auth;
}

function loadAuth() {
  const accessToken = localStorage.getItem("access_token") || "";
  const refreshToken = localStorage.getItem("refresh_token") || "";
  if (!refreshToken) throw new Error('Sign in to Codex in Pythona AI Setup and save this provider.');

  const idToken = localStorage.getItem("id_token") || "";
  const claims = jwtClaims(idToken) || {};
  const authClaim = claims["https://api.openai.com/auth"] || {};

  return {
    accessToken: accessToken,
    refreshToken: refreshToken,
    idToken: idToken || "",
    accountId: localStorage.getItem("account_id")
      || trimmedString(authClaim.chatgpt_account_id)
      || trimmedString(claims.chatgpt_account_id)
      || "",
    email: localStorage.getItem("email") || trimmedString(claims.email) || "",
    planType: localStorage.getItem("plan_type")
      || trimmedString(authClaim.chatgpt_plan_type)
      || trimmedString(claims.chatgpt_plan_type)
      || "",
    lastRefresh: localStorage.getItem("last_refresh") || ""
  };
}

function shouldRefresh(auth) {
  if (!auth.refreshToken) return false;
  if (!auth.accessToken) return true;

  const exp = jwtExpiry(auth.accessToken);
  if (exp && exp <= Date.now() + 5 * 60 * 1000) return true;

  const lastRefresh = Date.parse(auth.lastRefresh || "");
  if (!Number.isNaN(lastRefresh) && lastRefresh <= Date.now() - 55 * 60 * 1000) return true;
  return false;
}

async function refreshAuth(auth) {
  if (CHATGPT_ENABLED) return await CHATGPT_PROVIDER.refresh(auth);
  if (!auth.refreshToken) throw new Error("The ChatGPT token has no refresh token.");
  const latest = newerAuth(auth);
  if (latest) return latest;

  const response = await fetch(CODEX_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": CODEX_USER_AGENT, "originator": "codex_cli_rs" },
    body: JSON.stringify({
      client_id: CODEX_CLIENT_ID,
      grant_type: "refresh_token",
      refresh_token: auth.refreshToken
    })
  });

  if (!response.ok) {
    const current = newerAuth(auth);
    if (current) return current;
    throw new Error("ChatGPT token refresh failed (HTTP " + response.status + "). Sign in again in Pythona AI Setup.");
  }

  const value = await response.json();
  const current = newerAuth(auth);
  if (current) return current;
  const refreshed = withRefreshResponse(auth, value);
  saveAuth(refreshed);
  return refreshed;
}

function newerAuth(auth) {
  const current = loadAuth();
  if (current.refreshToken === auth.refreshToken) return null;
  const sameAccount = auth.accountId ? current.accountId === auth.accountId
    : auth.idToken && current.idToken === auth.idToken;
  if (!sameAccount) throw new Error("The Codex account changed. Retry the request.");
  return current;
}

function withRefreshResponse(auth, object) {
  const accessToken = trimmedString(object.access_token);
  if (!accessToken) throw new Error("ChatGPT token refresh returned no access token.");

  const nextIdToken = trimmedString(object.id_token) || auth.idToken;
  const claims = jwtClaims(nextIdToken) || {};
  const authClaim = claims["https://api.openai.com/auth"] || {};

  return {
    accessToken: accessToken,
    refreshToken: trimmedString(object.refresh_token) || auth.refreshToken,
    idToken: nextIdToken || "",
    accountId: trimmedString(authClaim.chatgpt_account_id)
      || trimmedString(claims.chatgpt_account_id)
      || auth.accountId
      || "",
    email: trimmedString(claims.email) || auth.email || "",
    planType: trimmedString(authClaim.chatgpt_plan_type)
      || trimmedString(claims.chatgpt_plan_type)
      || auth.planType
      || "",
    lastRefresh: new Date().toISOString()
  };
}

function saveAuth(auth) {
  localStorage.setItem("access_token", auth.accessToken);
  localStorage.setItem("refresh_token", auth.refreshToken);
  if (auth.idToken) localStorage.setItem("id_token", auth.idToken);
  if (auth.accountId) localStorage.setItem("account_id", auth.accountId);
  if (auth.email) localStorage.setItem("email", auth.email);
  if (auth.planType) localStorage.setItem("plan_type", auth.planType);
  localStorage.setItem("last_refresh", auth.lastRefresh);
}

function jwtExpiry(token) {
  const claims = jwtClaims(token);
  return claims && typeof claims.exp === "number" ? claims.exp * 1000 : null;
}

function jwtClaims(token) {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    return decodeBase64URLJSON(parts[1]);
  } catch (error) {
    return null;
  }
}

function decodeBase64URLJSON(value) {
  let encoded = value.replace(/-/g, "+").replace(/_/g, "/");
  while (encoded.length % 4 !== 0) encoded += "=";
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return JSON.parse(new TextDecoder().decode(bytes));
}

// Request building

function buildCodexRequest(ctx) {
  const namespace = checkpointNamespace(MODEL);
  const converted = buildResponsesInput(ctx, namespace);
  const webSearchEnabled = !!(ctx.preferences && ctx.preferences.webSearch);
  const body = {
    model: MODEL,
    input: converted.input,
    instructions: converted.instructions || "",
    prompt_cache_key: ctx.conversationId,
    stream: true,
    store: false
  };
  const reasoning = reasoningConfig();
  if (reasoning) body.reasoning = reasoning;
  const include = responseIncludeFields(webSearchEnabled);
  if (include.length > 0) body.include = include;

  const nativeTools = ctx.nativeTools || [];
  const tools = responsesTools(nativeTools, webSearchEnabled);
  if (tools.length > 0) {
    body.tools = tools;
    body.tool_choice = "auto";
  }
  if (CHATGPT_ENABLED) CHATGPT_PROVIDER.prepare(body);

  return {
    body: body,
    input: converted.input,
    coveredTranscriptIndex: converted.coveredTranscriptIndex,
    model: MODEL,
    allowedTools: new Set(nativeTools.map(tool => tool.name)),
    namespace: namespace
  };
}

async function fetchCodexResponse(auth, body) {
  if (CHATGPT_ENABLED) return await CHATGPT_PROVIDER.fetch(auth, body);
  return await fetch(CODEX_RESPONSES_URL, {
    method: "POST",
    headers: buildCodexHeaders(auth, "text/event-stream", body.prompt_cache_key),
    body: JSON.stringify(body)
  });
}

function buildCodexHeaders(auth, accept, sessionId) {
  const headers = {
    "Content-Type": "application/json",
    "Accept": accept,
    "Authorization": "Bearer " + auth.accessToken,
    "User-Agent": CODEX_USER_AGENT,
    "Version": CODEX_VERSION,
    "OpenAI-Beta": "responses=experimental",
    "originator": "codex_cli_rs",
    "session-id": sessionId
  };
  if (auth.accountId) headers["ChatGPT-Account-ID"] = auth.accountId;
  return headers;
}

function checkpointNamespace(model) {
  const prefix = CHATGPT_ENABLED ? CHATGPT_PROVIDER.checkpointPrefix() : "codex";
  return prefix + ":" + model + ":reasoning-" + reasoningCheckpointLabel() + ":" + CHECKPOINT_FORMAT;
}

function reasoningConfig() {
  const effort = trimmedString(REASONING_EFFORT);
  const summary = trimmedString(REASONING_SUMMARY);
  const config = {};
  if (effort) config.effort = effort;
  if (summary && effort !== "none") config.summary = summary;
  return Object.keys(config).length > 0 ? config : null;
}

function responseIncludeFields(webSearchEnabled) {
  const include = [];
  if (trimmedString(REASONING_EFFORT) !== "none") include.push("reasoning.encrypted_content");
  if (webSearchEnabled) include.push("web_search_call.action.sources");
  return include;
}

function reasoningCheckpointLabel() {
  return trimmedString(REASONING_EFFORT) || "default";
}

function responsesTools(nativeTools, includeWebSearch) {
  const converted = [];
  for (const tool of nativeTools) {
    if (!tool.name) continue;

    const item = {
      type: "function",
      name: tool.name,
      parameters: tool.inputSchema || { type: "object" },
      strict: false
    };
    if (tool.description) item.description = tool.description;
    converted.push(item);
  }

  if (includeWebSearch) converted.push({ type: "web_search" });
  return converted;
}

// Transcript conversion

function buildResponsesInput(ctx, namespace) {
  const instructions = typeof ctx.instructions === "string" ? ctx.instructions : "";
  const transcript = ctx.getTranscript();
  const records = Array.isArray(transcript.records) ? transcript.records : [];
  const checkpointInput = inputFromCheckpoints(records, namespace, MODEL);
  if (checkpointInput) {
    return {
      instructions: instructions,
      input: checkpointInput,
      coveredTranscriptIndex: maxRecordIndex(records)
    };
  }

  return {
    instructions: instructions,
    input: convertMessagesToResponsesInput(records
      .filter(record => record.type === "message")
      .map(record => record.message)),
    coveredTranscriptIndex: maxRecordIndex(records)
  };
}

function convertMessagesToResponsesInput(messages) {
  const input = [];

  for (const message of messages) {
    if (!message) continue;
    const role = message.role || "user";
    const content = textFromMessage(message);

    if (role === "tool") {
      const callId = message.tool_call_id || "";
      if (callId) {
        input.push({
          type: "function_call_output",
          call_id: callId,
          output: typeof content === "string" ? content : JSON.stringify(content)
        });
      }
    } else if (role === "assistant") {
      if (content !== "") input.push({ role: "assistant", content: content });
      for (const call of toolCallsFromMessage(message)) {
        if (!call.name) continue;
        input.push({
          type: "function_call",
          call_id: call.id || crypto.randomUUID(),
          name: call.name,
          arguments: call.inputJson || JSON.stringify(call.input || {}),
          status: "completed"
        });
      }
    } else {
      input.push({ role: role, content: content });
    }
  }

  return input;
}

function textFromMessage(message) {
  const content = textFromContent(message.content);
  if (content !== "" || !Array.isArray(message.parts)) return content;
  return message.parts
    .filter(part => part && part.type === "text" && typeof part.text === "string")
    .map(part => part.text)
    .join("");
}

function toolCallsFromMessage(message) {
  if (!Array.isArray(message.tool_calls)) return [];

  return message.tool_calls.map(call => {
    const fn = call.function || {};
    return {
      id: call.id || "",
      name: fn.name || "",
      inputJson: fn.arguments || "{}"
    };
  });
}

function inputFromCheckpoints(records, namespace, model) {
  let items = [];
  let lastCheckpointIndex = -1;
  let sawCheckpoint = false;

  for (let i = 0; i < records.length; i += 1) {
    const record = records[i];
    if (record.type !== "provider_record" || record.namespace !== namespace) continue;

    const checkpoint = parseCheckpoint(record.data, model);
    if (!checkpoint) continue;

    const index = recordIndex(record);
    if (typeof index !== "number"
        || checkpoint.coveredIndex < lastCheckpointIndex
        || checkpoint.coveredIndex >= index
        || !hasOnlyDerivedAssistantMessages(records, checkpoint.coveredIndex, index)) {
      continue;
    }
    items = items
      .concat(convertMessagesToResponsesInput(messagesBetween(records, lastCheckpointIndex, checkpoint.coveredIndex)))
      .concat(checkpoint.output);
    lastCheckpointIndex = index;
    sawCheckpoint = true;
  }

  if (!sawCheckpoint) return null;
  return items.concat(convertMessagesToResponsesInput(messagesAfter(records, lastCheckpointIndex)));
}

function parseCheckpoint(dataString, model) {
  const data = parseJSON(dataString);
  if (!data || data.format !== CHECKPOINT_FORMAT || data.model !== model) return null;
  if (!Array.isArray(data.output) || data.output.length === 0) return null;
  if (typeof data.covered_transcript_index !== "number") return null;
  return {
    output: data.output,
    coveredIndex: data.covered_transcript_index
  };
}

function hasOnlyDerivedAssistantMessages(records, afterIndex, beforeIndex) {
  for (let i = 0; i < records.length; i += 1) {
    const record = records[i];
    const index = recordIndex(record);
    if (record.type === "message" && index > afterIndex && index < beforeIndex
        && (!record.message || record.message.role !== "assistant")) {
      return false;
    }
  }
  return true;
}

function messagesBetween(records, afterIndex, throughIndex) {
  const messages = [];
  for (let i = 0; i < records.length; i += 1) {
    const record = records[i];
    const index = recordIndex(record);
    if (record.type === "message" && index > afterIndex && index <= throughIndex) {
      messages.push(record.message);
    }
  }
  return messages;
}

function messagesAfter(records, afterIndex) {
  const messages = [];
  for (let i = 0; i < records.length; i += 1) {
    const record = records[i];
    if (record.type === "message" && recordIndex(record) > afterIndex) {
      messages.push(record.message);
    }
  }
  return messages;
}

function maxRecordIndex(records) {
  let max = null;
  for (let i = 0; i < records.length; i += 1) {
    const index = recordIndex(records[i]);
    if (max === null || index > max) max = index;
  }
  return max;
}

function recordIndex(record) {
  return record.index;
}

function textFromContent(content) {
  if (content == null) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter(part => part && part.type === "text" && typeof part.text === "string")
      .map(part => part.text)
      .join("");
  }
  return JSON.stringify(content);
}

// Stream events

async function consumeResponseStream(response, emit, request) {
  const state = {
    request: request,
    terminalStatus: null,
    toolCalls: {},
    indexesByKey: {},
    pendingToolCallIndexes: {},
    finishReason: null,
    usage: null,
    citations: [],
    searchSources: {},
    outputItemsByIndex: {},
    outputItemIndexesByKey: {},
    reasoningItemsWithText: {},
    refusalText: {},
    responseOutput: null,
    citationTextBuffer: ""
  };

  try {
    for await (const event of readSSE(response)) {
      if (!event.data) continue;
      if (event.data === "[DONE]") break;
      handleResponseEvent(JSON.parse(event.data), state, emit);
      if (state.terminalStatus) break;
    }

    if (!state.terminalStatus) {
      throw new Error("Codex response stream ended before a terminal event.");
    }
    flushResponseState(state, emit);
  } catch (error) {
    // Preserve malformed or truncated marker candidates when the stream fails.
    flushCitationText(state, emit);
    throw error;
  }
}

function handleResponseEvent(obj, state, emit) {
  switch (obj.type) {
  case "response.output_text.delta":
    emitTextWithoutCitationMarkers(obj.delta, state, emit);
    break;
  case "response.refusal.delta":
    emitRefusal(obj.delta, obj.output_index, obj.content_index, false, state, emit);
    break;
  case "response.refusal.done":
    emitRefusal(obj.refusal, obj.output_index, obj.content_index, true, state, emit);
    break;
  case "response.reasoning_text.delta":
  case "response.reasoning_summary_text.delta":
  case "response.reasoning.delta":
  case "response.reasoning_summary.delta":
    emitReasoningDeltaFromEvent(obj, state, emit);
    break;
  case "response.output_item.added":
  case "response.output_item.done":
    handleOutputItemEvent(obj, state, emit);
    break;
  case "response.content_part.added":
  case "response.content_part.done":
  case "response.reasoning_text.done":
  case "response.reasoning_summary_text.done":
  case "response.reasoning.done":
  case "response.reasoning_summary.done":
  case "response.reasoning_summary_part.done":
    break;
  case "response.reasoning_summary_part.added":
    handleReasoningSummaryPartAdded(obj, state, emit);
    break;
  case "response.web_search_call.in_progress":
  case "response.web_search_call.searching":
    emit({
      type: "hosted_tool_call",
      id: webSearchId(obj, obj),
      name: "web_search",
      displayName: "Web Search",
      status: "running",
      parameters: webSearchParameters(obj)
    });
    break;
  case "response.web_search_call.completed":
    emit({
      type: "hosted_tool_call",
      id: webSearchId(obj, obj),
      name: "web_search",
      displayName: "Web Search",
      status: "success",
      parameters: webSearchParameters(obj)
    });
    break;
  case "response.function_call_arguments.delta":
    emitNativeToolPending(mergeFunctionArgumentsDelta(obj, state), state, emit);
    break;
  case "response.function_call_arguments.done":
    emitNativeToolPending(mergeFunctionArgumentsDone(obj, state), state, emit);
    break;
  case "response.completed":
    if (!obj.response) throw new Error("response.completed is missing response data.");
    if (CHATGPT_ENABLED && obj.response.status !== "completed") throw new Error("ChatGPT did not complete the response.");
    mergeCompletedResponse(obj.response, state, false);
    emitCompletedRefusals(obj.response, state, emit);
    state.terminalStatus = "completed";
    break;
  case "response.incomplete":
    if (CHATGPT_ENABLED) throw new Error(CHATGPT_PROVIDER.errorMessage(obj));
    if (!obj.response) throw new Error("response.incomplete is missing response data.");
    mergeCompletedResponse(obj.response, state, true);
    emitCompletedRefusals(obj.response, state, emit);
    state.terminalStatus = "incomplete";
    break;
  case "response.failed":
  case "error":
    if (CHATGPT_ENABLED) throw new Error(CHATGPT_PROVIDER.errorMessage(obj));
    throw new Error(errorMessage(obj));
  default:
    break;
  }
}

function emitRefusal(text, outputIndex, contentIndex, complete, state, emit) {
  if (typeof text !== "string" || !text) return;
  const key = (outputIndex ?? 0) + ":" + (contentIndex ?? 0);
  const previous = state.refusalText[key] || "";
  const delta = complete ? (text.startsWith(previous) ? text.slice(previous.length) : "") : text;
  state.refusalText[key] = previous + delta;
  emitTextWithoutCitationMarkers(delta, state, emit);
}

function emitCompletedRefusals(response, state, emit) {
  (response.output || []).forEach((item, outputIndex) => {
    (item.content || []).forEach((part, contentIndex) => {
      if (part.type === "refusal") emitRefusal(part.refusal, outputIndex, contentIndex, true, state, emit);
    });
  });
}

function emitTextWithoutCitationMarkers(delta, state, emit) {
  if (typeof delta !== "string" || delta.length === 0) return;

  let buffer = (typeof state.citationTextBuffer === "string" ? state.citationTextBuffer : "") + delta;
  const visibleChunks = [];

  while (buffer.length > 0) {
    const start = buffer.indexOf(CITATION_PREFIX);
    if (start < 0) {
      const retainedLength = trailingCitationPrefixLength(buffer);
      visibleChunks.push(buffer.slice(0, buffer.length - retainedLength));
      buffer = buffer.slice(buffer.length - retainedLength);
      break;
    }

    visibleChunks.push(buffer.slice(0, start));
    buffer = buffer.slice(start);
    const nestedStart = buffer.indexOf(CITATION_PREFIX, CITATION_PREFIX.length);
    const stopSearchEnd = Math.min(
      buffer.length,
      CITATION_MAX_CANDIDATE_LENGTH,
      nestedStart >= 0 ? nestedStart : buffer.length
    );
    const relativeStop = buffer
      .slice(CITATION_PREFIX.length, stopSearchEnd)
      .indexOf(CITATION_STOP);
    const stop = relativeStop >= 0 ? CITATION_PREFIX.length + relativeStop : -1;

    if (stop < 0 && nestedStart >= 0) {
      visibleChunks.push(buffer.slice(0, nestedStart));
      buffer = buffer.slice(nestedStart);
      continue;
    }

    if (stop < 0) {
      if (buffer.length > CITATION_MAX_CANDIDATE_LENGTH) {
        const retainedLength = trailingCitationPrefixLength(buffer);
        visibleChunks.push(buffer.slice(0, buffer.length - retainedLength));
        buffer = buffer.slice(buffer.length - retainedLength);
      }
      break;
    }

    const marker = buffer.slice(0, stop + CITATION_STOP.length);
    // URL annotations are rendered in the Sources footer today. If Pythona adds
    // inline citations later, resolve this validated marker here instead of dropping it.
    if (!isValidCitationMarker(marker)) visibleChunks.push(marker);
    buffer = buffer.slice(stop + CITATION_STOP.length);
  }

  state.citationTextBuffer = buffer;
  const visibleText = visibleChunks.join("");
  if (visibleText.length > 0) emit({ type: "text", delta: visibleText });
}

function trailingCitationPrefixLength(text) {
  const maxLength = Math.min(text.length, CITATION_PREFIX.length - 1);
  for (let length = maxLength; length > 0; length -= 1) {
    if (text.slice(text.length - length) === CITATION_PREFIX.slice(0, length)) return length;
  }
  return 0;
}

function isValidCitationMarker(marker) {
  if (!marker.startsWith(CITATION_PREFIX) || !marker.endsWith(CITATION_STOP)) return false;

  const body = marker.slice(CITATION_PREFIX.length, marker.length - CITATION_STOP.length);
  const parts = body
    .split(CITATION_DELIMITER)
    .map(part => part.trim())
    .filter(Boolean);
  if (parts.length === 0) return false;

  if (CITATION_LINE_LOCATOR_RE.test(parts[parts.length - 1])) parts.pop();
  return parts.length > 0 && parts.every(part => CITATION_SOURCE_ID_RE.test(part));
}

function flushCitationText(state, emit) {
  const pending = typeof state.citationTextBuffer === "string" ? state.citationTextBuffer : "";
  state.citationTextBuffer = "";
  if (pending.length > 0) emit({ type: "text", delta: pending });
}

function handleOutputItemEvent(obj, state, emit) {
  const item = obj.item || {};
  rememberOutputItem(obj, state);
  if (item.type === "web_search_call") {
    const id = webSearchId(item, obj);
    emit({
      type: "hosted_tool_call",
      id: id,
      name: "web_search",
      displayName: "Web Search",
      status: webSearchStatus(item),
      parameters: webSearchParameters(item)
    });

    const sources = sourcesFromWebSearchItem(item);
    if (sources.length > 0) {
      state.searchSources[id] = dedupedSources((state.searchSources[id] || []).concat(sources));
    }
  } else if (item.type === "function_call") {
    const index = toolCallIndex(obj, item, state, false);
    mergeFunctionCall(item, state, index, obj.type === "response.output_item.done");
    emitNativeToolPending(index, state, emit);
  }
}

function emitReasoningDeltaFromEvent(obj, state, emit) {
  if (emitReasoningDelta(obj.delta, emit)) {
    const itemId = firstString(obj.item_id, obj.output_item_id);
    if (itemId) state.reasoningItemsWithText[itemId] = true;
  }
}

function handleReasoningSummaryPartAdded(obj, state, emit) {
  if (typeof obj.summary_index !== "number" || obj.summary_index <= 0) return;
  const itemId = firstString(obj.item_id, obj.output_item_id);
  if (!itemId || !state.reasoningItemsWithText[itemId]) return;
  emitReasoningDelta("\n\n", emit);
}

function emitReasoningDelta(delta, emit) {
  if (typeof delta === "string" && delta.length > 0) {
    emit({ type: "reasoning", delta: delta });
    return true;
  } else if (delta && typeof delta.text === "string" && delta.text.length > 0) {
    emit({ type: "reasoning", delta: delta.text });
    return true;
  }
  return false;
}

function mergeFunctionArgumentsDelta(obj, state) {
  validateToolNamespace(obj);
  const index = toolCallIndex(obj, null, state, true);
  const cur = state.toolCalls[index] || { id: "", name: "", args: "" };
  if (obj.call_id) cur.id = obj.call_id;
  if (obj.name) cur.name = obj.name;
  if (typeof obj.delta === "string") cur.args += obj.delta;
  state.toolCalls[index] = cur;
  return index;
}

function mergeFunctionArgumentsDone(obj, state) {
  validateToolNamespace(obj);
  const index = toolCallIndex(obj, null, state, true);
  const cur = state.toolCalls[index] || { id: "", name: "", args: "" };
  if (obj.call_id) cur.id = obj.call_id;
  if (obj.name) cur.name = obj.name;
  if (typeof obj.arguments === "string") cur.args = obj.arguments;
  state.toolCalls[index] = cur;
  return index;
}

function emitNativeToolPending(index, state, emit) {
  if (state.pendingToolCallIndexes[index]) return;
  state.pendingToolCallIndexes[index] = true;

  // Announce the call as soon as argument generation starts so native can keep its
  // pending animation visible. Do not forward partial JSON here: flushResponseState
  // appends the terminal, authoritative arguments once after response.completed.
  emit({
    type: "native_tool_call_delta",
    index: index
  });
}

function mergeCompletedResponse(response, state, incomplete) {
  state.usage = usageFromResponse(response) || state.usage;
  state.finishReason = finishReasonFromResponse(response) || (incomplete ? "length" : state.finishReason);
  state.citations = state.citations.concat(citationsFromResponse(response));
  if (Array.isArray(response.output) && response.output.length > 0) state.responseOutput = response.output;
  mergeSearchSources(response, state.searchSources);
  mergeCompletedFunctionCalls(response, state);
}

function flushResponseState(state, emit) {
  flushCitationText(state, emit);

  if (state.terminalStatus === "completed") {
    for (const index of sortedNumericKeys(state.toolCalls)) {
      const call = state.toolCalls[index];
      if (CHATGPT_ENABLED && !isNonEmptyString(call.id)) throw new Error("ChatGPT returned a tool without a call ID.");
      if (!state.request.allowedTools.has(call.name)) throw new Error("The provider returned an unknown tool.");
      const input = JSON.parse(call.args);
      if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("The provider returned invalid tool arguments.");
    }
    for (const index of sortedNumericKeys(state.toolCalls)) {
      const call = state.toolCalls[index];
      emit({
        type: "native_tool_call_delta",
        index: index,
        id: call.id || undefined,
        name: call.name || undefined,
        inputJsonDelta: call.args
      });
    }
  }

  for (const id of Object.keys(state.searchSources).sort()) {
    const sources = dedupedSources(state.searchSources[id]);
    if (sources.length > 0) {
      emit({ type: "sources", hostedToolCallId: id, sources: sources });
    }
  }

  const citations = dedupedSources(state.citations);
  if (citations.length > 0) emit({ type: "sources", sources: citations });

  if (state.usage) {
    emit({
      type: "usage",
      prompt: state.usage.promptTokens,
      completion: state.usage.completionTokens
    });
  }

  emitCheckpoint(state, emit);

  if (state.finishReason) emit({ type: "finish", reason: state.finishReason });
}

function toolCallIndex(event, item, state, preferExistingUnkeyed) {
  const keys = [
    event.call_id,
    event.item_id,
    event.output_item_id,
    item && item.call_id,
    item && item.id
  ].filter(isNonEmptyString);

  for (const key of keys) {
    if (state.indexesByKey[key] !== undefined) return state.indexesByKey[key];
  }

  if (keys.length > 0) {
    const unkeyedIndex = existingUnkeyedCallIndex(state, item);
    if (unkeyedIndex !== null) {
      for (const key of keys) state.indexesByKey[key] = unkeyedIndex;
      return unkeyedIndex;
    }
  }

  if (typeof event.output_index === "number") {
    for (const key of keys) state.indexesByKey[key] = event.output_index;
    return event.output_index;
  }

  const existing = sortedNumericKeys(state.toolCalls);
  if (preferExistingUnkeyed && keys.length === 0 && existing.length > 0) {
    return existing[existing.length - 1];
  }

  const index = existing.length > 0 ? existing[existing.length - 1] + 1 : 0;
  for (const key of keys) state.indexesByKey[key] = index;
  return index;
}

function existingUnkeyedCallIndex(state, item) {
  const itemName = item && item.name;
  const candidates = [];
  for (const index of sortedNumericKeys(state.toolCalls)) {
    const call = state.toolCalls[index];
    if (!call.id && (!call.name || !itemName || call.name === itemName)) candidates.push(index);
  }
  return candidates.length === 1 ? candidates[0] : null;
}

function validateToolNamespace(item) {
  if (CHATGPT_ENABLED && item.namespace != null && item.namespace !== "pythona") {
    throw new Error("ChatGPT returned an unknown tool namespace.");
  }
}

function mergeFunctionCall(item, state, index, replaceArguments) {
  validateToolNamespace(item);
  const cur = state.toolCalls[index] || { id: "", name: "", args: "" };
  if (item.call_id) cur.id = item.call_id;
  if (item.name) cur.name = item.name;
  if (item.arguments) {
    if (replaceArguments) cur.args = item.arguments;
    else if (!cur.args) cur.args = item.arguments;
  }
  state.toolCalls[index] = cur;
}

function mergeCompletedFunctionCalls(response, state) {
  const output = response.output || [];
  for (let i = 0; i < output.length; i += 1) {
    const item = output[i];
    if (item.type !== "function_call") continue;
    const index = toolCallIndex({ output_index: i }, item, state, false);
    mergeFunctionCall(item, state, index, true);
  }
}

function usageFromResponse(response) {
  const usage = response.usage || {};
  const prompt = firstNumber(usage.input_tokens, usage.prompt_tokens);
  const completion = firstNumber(usage.output_tokens, usage.completion_tokens);
  if (prompt === null || completion === null) return null;
  return { promptTokens: prompt, completionTokens: completion };
}

function finishReasonFromResponse(response) {
  if (response.status === "incomplete") {
    const reason = response.incomplete_details && response.incomplete_details.reason;
    return reason === "max_output_tokens" ? "length" : reason;
  }
  if (response.status === "completed") return "stop";
  return null;
}

// Provider checkpoints

function rememberOutputItem(event, state) {
  if (!event.item) return;
  const key = outputItemKey(event);
  let index = null;

  if (typeof event.output_index === "number") {
    index = event.output_index;
  } else if (key && state.outputItemIndexesByKey[key] !== undefined) {
    index = state.outputItemIndexesByKey[key];
  } else {
    const keys = sortedNumericKeys(state.outputItemsByIndex);
    index = keys.length > 0 ? keys[keys.length - 1] + 1 : 0;
  }

  state.outputItemsByIndex[index] = event.item;
  if (key) state.outputItemIndexesByKey[key] = index;
}

function outputItemKey(event) {
  const item = event.item || {};
  const key = event.output_item_id || event.item_id || item.id || item.call_id;
  return typeof key === "string" && key.length > 0 ? item.type + ":" + key : "";
}

function checkpointCandidateItems(state) {
  if (Array.isArray(state.responseOutput) && state.responseOutput.length > 0) {
    return {
      output: state.responseOutput,
      source: "response.output"
    };
  }

  const streamedItems = sortedNumericKeys(state.outputItemsByIndex).map(index => state.outputItemsByIndex[index]);
  if (streamedItems.length > 0) {
    return {
      output: streamedItems,
      source: "response.output_item"
    };
  }

  return { output: [], source: "none" };
}

function emitCheckpoint(state, emit) {
  const checkpoint = checkpointCandidateItems(state);
  const output = checkpoint.output;
  if (state.terminalStatus !== "completed" || !state.request || output.length === 0) return;

  emit({
    type: "provider_record",
    namespace: state.request.namespace,
    data: JSON.stringify({
      format: CHECKPOINT_FORMAT,
      model: state.request.model,
      source: checkpoint.source,
      covered_transcript_index: state.request.coveredTranscriptIndex,
      output: output
    })
  });
}

// Web search and sources

function citationsFromResponse(response) {
  const citations = [];
  for (const item of response.output || []) {
    if (item.type !== "message") continue;
    for (const content of item.content || []) {
      for (const annotation of content.annotations || []) {
        if (annotation.type !== "url_citation" || !annotation.url) continue;
        citations.push({ title: annotation.title || annotation.url, url: annotation.url });
      }
    }
  }
  return citations;
}

function mergeSearchSources(response, out) {
  for (const item of response.output || []) {
    if (item.type !== "web_search_call") continue;
    const id = webSearchId(item, item);
    const sources = sourcesFromWebSearchItem(item);
    if (sources.length > 0) out[id] = dedupedSources((out[id] || []).concat(sources));
  }
}

function sourcesFromWebSearchItem(item) {
  const action = item.action || {};
  const sourceDicts = action.sources || item.sources || item.results || [];
  const sources = [];
  for (const source of sourceDicts) {
    if (!source.url) continue;
    sources.push({
      title: source.title || source.text || source.url,
      url: source.url,
      snippet: source.snippet || undefined,
      pageAge: source.page_age || source.pageAge || undefined
    });
  }
  return sources;
}

function webSearchId(value, fallback) {
  return firstString(
    value.id,
    value.item_id,
    value.output_item_id,
    fallback.item_id,
    fallback.output_item_id
  ) || "web_search";
}

function webSearchStatus(item) {
  if (item.status) {
    if (item.status === "completed") return "success";
    if (item.status === "failed") return "error";
    return "running";
  }
  return "running";
}

function webSearchParameters(item) {
  const payload = webSearchActionParameters(item) || webSearchParameterFields(item);
  return payload ? JSON.stringify(payload, null, 2) : undefined;
}

function webSearchActionParameters(item) {
  if (!item.action) return null;
  const action = shallowCopy(item.action);
  delete action.sources;
  delete action.results;
  return Object.keys(action).length > 0 ? action : null;
}

function webSearchParameterFields(item) {
  const out = {};
  for (const key of ["query", "queries", "search_context_size", "user_location"]) {
    if (item[key] !== undefined) out[key] = item[key];
  }
  return Object.keys(out).length > 0 ? out : null;
}

// SSE parser

async function* readSSE(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = [];

  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      appendBytes(buffer, next.value);

      let boundary;
      while ((boundary = findSSEBoundary(buffer)) !== null) {
        const blockBytes = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        if (blockBytes.length > 0) yield parseSSEBlock(decoder.decode(new Uint8Array(blockBytes)));
      }
    }

    if (buffer.length > 0) yield parseSSEBlock(decoder.decode(new Uint8Array(buffer)));
  } finally {
    reader.releaseLock();
  }
}

function appendBytes(out, chunk) {
  for (let i = 0; i < chunk.length; i += 1) out.push(chunk[i]);
}

function findSSEBoundary(bytes) {
  for (let i = 0; i < bytes.length - 1; i += 1) {
    if (bytes[i] === 10 && bytes[i + 1] === 10) return { index: i, length: 2 };
    if (bytes[i] === 13 && bytes[i + 1] === 13) return { index: i, length: 2 };
    if (
      i < bytes.length - 3
      && bytes[i] === 13
      && bytes[i + 1] === 10
      && bytes[i + 2] === 13
      && bytes[i + 3] === 10
    ) {
      return { index: i, length: 4 };
    }
  }
  return null;
}

function parseSSEBlock(block) {
  const event = {};
  const data = [];
  for (const line of block.split(/\r\n|\r|\n/)) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon >= 0 ? line.slice(0, colon) : line;
    let value = colon >= 0 ? line.slice(colon + 1) : "";
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "data") data.push(value);
    else if (field === "event") event.event = value;
    else if (field === "id") event.id = value;
    else if (field === "retry") event.retry = Number(value);
  }
  event.data = data.join("\n");
  return event;
}

// Utilities

function parseJSON(value) {
  if (typeof value !== "string" || value.length === 0) return null;
  try {
    return JSON.parse(value);
  } catch (error) {
    return null;
  }
}

function errorMessage(event) {
  if (event.error && event.error.message) return event.error.message;
  if (event.response && event.response.error && event.response.error.message) {
    return event.response.error.message;
  }
  return JSON.stringify(event);
}

function dedupedSources(sources) {
  const seen = {};
  const result = [];
  for (const source of Array.isArray(sources) ? sources : []) {
    if (!source || !source.url || seen[source.url]) continue;
    seen[source.url] = true;
    result.push(source);
  }
  return result;
}

function sortedNumericKeys(object) {
  return Object.keys(object || {})
    .map(function(key) { return Number(key); })
    .filter(function(value) { return !Number.isNaN(value); })
    .sort(function(a, b) { return a - b; });
}

function shallowCopy(object) {
  const out = {};
  for (const key of Object.keys(object || {})) out[key] = object[key];
  return out;
}

function trimmedString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function isNonEmptyString(value) {
  return trimmedString(value) !== null;
}

function firstString() {
  for (let i = 0; i < arguments.length; i += 1) {
    const value = trimmedString(arguments[i]);
    if (value !== null) return value;
  }
  return null;
}

function firstNumber() {
  for (let i = 0; i < arguments.length; i += 1) {
    if (typeof arguments[i] === "number" && Number.isFinite(arguments[i])) return arguments[i];
  }
  return null;
}
