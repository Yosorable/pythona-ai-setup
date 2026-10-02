// Public ChatGPT-plan transport. Bundled with the shared Responses engine.
// localStorage is Pythona's native per-provider store, not WebKit browser storage.
const CHATGPT_PROVIDER = (() => {
  const key = "chatgpt_auth";
  const endpoint = "https://api.openai.com/v1/responses";
  const tokenEndpoint = "https://auth.openai.com/api/accounts/oauth/token";
  const resource = "https://api.openai.com/v1";
  const userAgent = "PythonaAISetup/1.0 (iOS)";
  const planScope = "chatgpt.tokens.use.direct";
  const signIn = "Sign in to ChatGPT in Pythona AI Setup and save this provider.";

  function load(requireRefresh = true) {
    let value;
    try { value = JSON.parse(localStorage.getItem(key) || "{}"); }
    catch (_) { throw new Error(signIn); }
    if (!value || typeof value !== "object" || !value.client_id || value.client_id === "dynamic_agent_client"
        || (requireRefresh && !value.refresh_token) || !value.subject || value.issuer !== "https://auth.openai.com") throw new Error(signIn);
    return value;
  }

  function permitted(auth) {
    return typeof auth.scope === "string" && auth.scope.split(/\s+/).includes(planScope);
  }

  function currentLogin(auth) {
    const current = load(false);
    const sameGrant = auth.local_grant_id ? current.local_grant_id === auth.local_grant_id
      : auth.id_token && current.id_token === auth.id_token;
    if (current.client_id !== auth.client_id || current.subject !== auth.subject || !sameGrant) {
      throw new Error("The ChatGPT account changed. Retry the request.");
    }
    return current;
  }

  async function refresh(auth) {
    // Never replace a newer account or refresh token from another invocation.
    const latest = currentLogin(auth);
    if (!latest.refresh_token) throw new Error(signIn);
    if (latest.refresh_token !== auth.refresh_token) return latest;
    const fields = { grant_type: "refresh_token", client_id: auth.client_id,
      refresh_token: auth.refresh_token, resource };
    const response = await fetch(tokenEndpoint, { method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "User-Agent": userAgent },
      body: Object.entries(fields).map(([name, value]) => encodeURIComponent(name) + "=" + encodeURIComponent(value)).join("&") });
    if (!response.ok) {
      let code;
      try { const body = await response.json(); code = typeof body.error === "string" ? body.error : body.error?.code; } catch (_) {}
      const current = currentLogin(auth);
      if (current.refresh_token && current.refresh_token !== auth.refresh_token) return current;
      if (["invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
        "refresh_token_invalidated", "refresh_token_invalid", "refresh_token_reused"].includes(code)) {
        if (current.refresh_token === auth.refresh_token) {
          const retained = { ...current };
          for (const name of ["access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at", "scope"]) delete retained[name];
          localStorage.setItem(key, JSON.stringify(retained));
        }
        throw new Error("ChatGPT authorization expired. " + signIn);
      }
      throw new Error("ChatGPT token refresh failed (HTTP " + response.status + "). Try again later.");
    }
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error("ChatGPT returned an invalid token response. " + signIn); }
    if (!body || typeof body !== "object" || typeof body.access_token !== "string" || !body.access_token || typeof body.refresh_token !== "string"
        || !body.refresh_token || typeof body.expires_in !== "number" || !Number.isFinite(body.expires_in)
        || body.expires_in <= 0 || body.expires_in > 86400
        || (body.token_type && (typeof body.token_type !== "string" || body.token_type.toLowerCase() !== "bearer"))) {
      throw new Error("ChatGPT returned an incomplete token response. " + signIn);
    }
    const current = currentLogin(auth);
    if (current.refresh_token && current.refresh_token !== auth.refresh_token) return current;
    // local_grant_id survives token clearing, so a late successful refresh can
    // recover that login without reviving a deleted provider or a replaced login.
    const next = { ...auth, ...current, access_token: body.access_token, refresh_token: body.refresh_token,
      expires_at: String(Date.now() / 1000 + body.expires_in),
      earliest_refresh_at: body.earliest_refresh_at == null ? "" : String(body.earliest_refresh_at),
      scope: typeof body.scope === "string" ? body.scope : current.scope || auth.scope };
    // Retain the identity and ID token verified during authorization. A single
    // write keeps tokens and expiry together across fresh JavaScript contexts.
    localStorage.setItem(key, JSON.stringify(next));
    return next;
  }

  async function freshAuth() {
    let auth = load();
    const expires = Number(auth.expires_at);
    if (!auth.access_token || !Number.isFinite(expires) || expires <= Date.now() / 1000 + 60) auth = await refresh(auth);
    if (!permitted(auth)) throw new Error("Allow ChatGPT plan usage by signing in again in Pythona AI Setup.");
    return auth;
  }

  function errorMessage(object) {
    const error = object?.response?.error || object?.error || object;
    const code = error?.code || object?.code;
    const messages = {
      subscription_sharing_user_not_eligible: "ChatGPT plan usage is unavailable for this account or workspace.",
      subscription_sharing_usage_limit_exceeded: "ChatGPT usage limit reached. Manage usage at https://chatgpt.com/settings/usage.",
      subscription_sharing_usage_unavailable: "ChatGPT usage is temporarily unavailable. Try again later.",
      subscription_sharing_user_unavailable: "This ChatGPT account is temporarily unavailable. Try again later.",
      subscription_sharing_unsupported_capability: "This model, input, or tool is not supported by ChatGPT plan usage.",
      subscription_sharing_route_not_supported: "This API route is not supported by ChatGPT plan usage.",
      subscription_sharing_invalid_user: "ChatGPT could not validate this account. " + signIn,
      chatpass_v2_scope_not_authorized: "ChatGPT plan usage was not authorized. " + signIn,
      chatpass_v2_invalid_authorization_context: "ChatGPT plan usage was not authorized. " + signIn
    };
    if (messages[code]) return messages[code];
    if (typeof error?.message === "string") return error.message;
    if (typeof object?.detail === "string") return object.detail;
    return "ChatGPT did not complete the response.";
  }

  return { freshAuth, refresh, errorMessage,
    settingsKey: "chatgpt_settings",
    checkpointPrefix: () => "chatgpt:" + load().client_id,
    prepare(body) {
      body.input = body.input.map(item => {
        if (item.role === "system") return { ...item, role: "developer" };
        if (item.type === "function_call" && item.namespace == null) return { ...item, namespace: "pythona" };
        return item;
      });
      if (body.tools) {
        const functions = body.tools.filter(tool => tool.type === "function");
        body.tools = [...(functions.length ? [{ type: "namespace", name: "pythona",
          description: "Tools available in Pythona.", tools: functions }] : []),
          ...body.tools.filter(tool => tool.type !== "function")];
      }
    },
    async fetch(auth, body) {
      if (!permitted(auth)) throw new Error("Allow ChatGPT plan usage by signing in again in Pythona AI Setup.");
      return await fetch(endpoint, { method: "POST", body: JSON.stringify(body),
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream",
          "Authorization": "Bearer " + auth.access_token, "User-Agent": userAgent } });
    },
    async httpError(response) {
      let detail = "";
      try { detail = errorMessage(await response.json()); } catch (_) {}
      return "ChatGPT request failed (HTTP " + response.status + "). " + detail;
    }
  };
})();
