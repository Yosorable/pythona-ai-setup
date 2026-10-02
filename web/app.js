(() => {
  "use strict";
  const { language, strings, preview, default_mlx_model, default_codex_model } = window.SETUP;
  const $ = id => document.getElementById(id);
  const t = (key, values = {}) => strings[key].replace(/\{(\w+)(?::\.1f)?\}/g, (match, key) =>
    match.includes(":.1f") ? Number(values[key]).toFixed(1) : String(values[key]));
  document.documentElement.lang = language;
  document.title = "Pythona AI Setup";
  document.querySelectorAll("[data-i18n]").forEach(node => { node.textContent = t(node.dataset.i18n); });
  $("preview-note").hidden = !preview;
  let nextID = 0, state = null, working = false, stopped = false, polling = false, profilesSignature = "", modelsSignature = "";
  const pending = new Map();
  const demo = { revision: 0, page: "home", editor_id: null, profiles: [], settings: null,
    availability: {}, test: {}, auth: {}, accounts: {}, models: { items: [], message: "" }, plan_notice: false, notice: "", closed: false };
  let demoID = 0, demoTimer = null, demoLoginTimer = null;
  const defaultName = backend => ({ apple_fm: "Apple On-Device Model", mlx_lm: "Qwen3-1.7B (MLX)", codex: "Codex", chatgpt: "ChatGPT" })[backend];
  const cloud = backend => ["codex", "chatgpt"].includes(backend);
  const backendName = backend => ({ apple_fm: t("apple_backend"), mlx_lm: "MLX-LM", codex: "Codex", chatgpt: "ChatGPT" })[backend];
  const authenticating = auth => ["starting", "waiting", "exchanging"].includes(auth?.phase);
  function demoOpen(settings, page) {
    clearTimeout(demoTimer);
    clearTimeout(demoLoginTimer);
    demo.page = page;
    demo.plan_notice = false;
    demo.models = { items: [], message: "" };
    demo.settings = JSON.parse(JSON.stringify(settings));
    demo.editor_id = "editor-" + (++demoID);
    demo.notice = "";
    demo.test = { running: false, message: "", text: "", memory: "" };
    demo.auth = { phase: "idle", signed_in: false, ready: false, message: t(settings.backend === "chatgpt" ? "chatgpt_auth_required" : "auth_required"), ...demo.accounts[settings.id] };
    demo.availability = cloud(settings.backend) ? { kind: "info", message: t(settings.backend + "_cloud") }
      : settings.backend === "mlx_lm" ? { kind: "available", message: t("mlx_ready") }
      : { kind: "unavailable", message: t("unavailable", { reason: t("reason_DEVICE_NOT_ELIGIBLE") }) };
  }
  function demoHome() {
    clearTimeout(demoTimer);
    clearTimeout(demoLoginTimer);
    demo.page = "home"; demo.plan_notice = false; demo.settings = null; demo.editor_id = null;
  }
  async function previewCall(action, payload) {
    if (["install", "test", "login"].includes(action)) {
      if (payload.editor_id !== demo.editor_id) throw new Error("This form has closed.");
      if (action !== "login") {
        if (!payload.settings.name.trim()) throw new Error(t("empty_name"));
        if (payload.settings.backend === "mlx_lm" && !/^[\w.-]+\/[\w.-]+$/.test(payload.settings.model_id.trim())) throw new Error(t("invalid_model_id"));
        if (cloud(payload.settings.backend) && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(payload.settings.model_id.trim())) throw new Error(t(payload.settings.backend === "chatgpt" ? "chatgpt_invalid_model" : "invalid_codex_model"));
      }
    }
    if (action === "new") { demoHome(); demo.page = "choose"; demo.notice = ""; }
    if (action === "select_provider") {
      const backend = payload.backend;
      if (demo.page !== "choose" || !defaultName(backend)) throw new Error("Choose a provider first.");
      demoOpen({ id: "profile-" + (++demoID), name: defaultName(backend), backend,
        model_id: backend === "mlx_lm" ? default_mlx_model : cloud(backend) ? default_codex_model : "",
        ...(cloud(backend) ? { reasoning_effort: "medium" } : { groups: { files: true, browser: false, python: false } }),
        provider_id: null }, "new");
    }
    if (action === "edit") demoOpen(demo.profiles.find(profile => profile.id === payload.profile_id), "edit");
    if (action === "acknowledge_plan") { demo.plan_notice = false; demo.auth.plan_notice_seen = true; }
    if (action === "manage_usage") window.open("https://chatgpt.com/settings/usage", "_blank", "noopener");
    if (action === "back") { demoHome(); demo.notice = ""; }
    if (action === "install") {
      if (cloud(payload.settings.backend) && !demo.auth.ready) {
        throw new Error(t(payload.settings.backend === "chatgpt"
          ? demo.auth.signed_in ? "chatgpt_plan_required" : "chatgpt_auth_required" : "auth_required"));
      }
      const added = demo.page === "new";
      const profile = { ...demo.settings, ...payload.settings, installation_status: "installed" };
      profile.provider_id ||= "preview-" + profile.id;
      const index = demo.profiles.findIndex(value => value.id === profile.id);
      if (index < 0) demo.profiles.push(profile); else demo.profiles[index] = profile;
      if (cloud(profile.backend)) demo.accounts[profile.id] = { ...demo.auth, phase: "idle" };
      demoHome();
      demo.notice = t(added ? "provider_added" : "provider_updated");
    }
    if (action === "test") {
      if (cloud(payload.settings.backend) && !demo.auth.signed_in) throw new Error(t(payload.settings.backend === "chatgpt" ? "chatgpt_auth_required" : "auth_required"));
      if (!payload.prompt.trim()) throw new Error(t("your_message"));
      Object.assign(demo.settings, payload.settings);
      demo.test = { running: true, message: t("test_running"), text: "", memory: "" };
      demoTimer = setTimeout(() => {
        demo.test = { running: false, message: t("test_received", { seconds: 0.6 }),
          text: t("preview_note") + "\n\n" + payload.prompt, memory: "" };
      }, 600);
    }
    if (action === "cancel_test") {
      clearTimeout(demoTimer);
      demo.test = { running: false, message: t("test_cancelled"), text: "", memory: "" };
    }
    if (action === "login") {
      Object.assign(demo.settings, payload.settings);
      demo.auth = { ...demo.auth, phase: "waiting", message: t("auth_waiting") };
      demoLoginTimer = setTimeout(() => {
        demo.auth = { ...demo.auth, phase: "success", signed_in: true, ready: true, email: "demo@example.test", plan_type: "plus", plan_enabled: true, message: t("auth_success") };
        if (demo.settings.backend === "chatgpt") {
          demo.plan_notice = !demo.auth.plan_notice_seen;
          demo.models = { items: [{ id: default_codex_model, name: default_codex_model }], message: "" };
        }
      }, 600);
    }
    if (action === "cancel_login") {
      clearTimeout(demoLoginTimer);
      demo.auth = { ...demo.auth, phase: "cancelled", message: t("auth_cancelled") };
    }
    return JSON.parse(JSON.stringify({ ...demo, revision: ++demo.revision }));
  }

  function call(action, payload) {
    if (preview) return previewCall(action, payload);
    return new Promise((resolve, reject) => {
      const id = ++nextID;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error("The settings bridge did not respond.")); }, 15000);
      pending.set(id, { resolve, reject, timer });
      try { window.webkit.messageHandlers.setup.postMessage(JSON.stringify({ id, action, payload })); }
      catch (error) { clearTimeout(timer); pending.delete(id); reject(error); }
    });
  }
  window.setupBridge = { receive(response) {
    const request = pending.get(response.id);
    if (!request) return;
    clearTimeout(request.timer);
    pending.delete(response.id);
    if (response.error) request.reject(new Error(response.error));
    else request.resolve(response.result);
  }};

  function formPayload() {
    const backend = state.settings.backend;
    return { editor_id: state.editor_id, settings: { name: $("name").value, backend,
      model_id: backend === "mlx_lm" ? $("model-id").value : cloud(backend) ? $("codex-model").value : "",
      ...(cloud(backend) ? { reasoning_effort: $("reasoning-effort").value } : {}),
      ...(cloud(backend) ? {} : { groups: Object.fromEntries(["files", "browser", "python"].map(key => [key, $(key).checked])) }) } };
  }
  function showError(error) {
    $("error").textContent = error ? String(error.message || error) : "";
    $("error").hidden = !error;
    if (error) $("error").scrollIntoView({ block: "nearest" });
  }
  function render(value) {
    if (!value || (state && value.revision < state.revision)) return;
    const switched = !state || state.editor_id !== value.editor_id || state.page !== value.page;
    const newForm = !state || state.editor_id !== value.editor_id;
    state = value;
    $("home").hidden = state.page !== "home";
    $("choose").hidden = state.page !== "choose";
    $("detail").hidden = !["new", "edit"].includes(state.page);
    $("plan-notice").hidden = !state.plan_notice;
    document.documentElement.dataset.page = state.page;
    const signature = JSON.stringify([state.profiles, working, state.closed]);
    if (signature !== profilesSignature) {
      profilesSignature = signature;
      $("profiles").replaceChildren(...state.profiles.map(profile => {
        const button = document.createElement("button");
        button.className = "profile";
        button.dataset.id = profile.id;
        button.disabled = working || state.closed;
        const copy = document.createElement("span"), name = document.createElement("span"), detail = document.createElement("span"), chevron = document.createElement("span");
        copy.className = "profile-copy"; name.className = "profile-name"; detail.className = "profile-detail"; chevron.className = "chevron";
        name.textContent = profile.name;
        detail.textContent = profile.backend === "apple_fm" ? t("apple_backend")
          : backendName(profile.backend) + " · " + profile.model_id;
        if (profile.installation_status !== "installed") detail.textContent += " · " + t("status_" + profile.installation_status);
        chevron.textContent = "›";
        copy.append(name, detail); button.append(copy, chevron);
        button.addEventListener("click", () => perform("edit", { profile_id: profile.id }));
        return button;
      }));
    }
    $("empty-profiles").hidden = state.profiles.length > 0;
    $("refresh").hidden = !state.profiles.length;
    for (const id of ["home-notice", "detail-notice"]) { $(id).textContent = state.notice; $(id).hidden = !state.notice; }
    if (state.settings) {
      if (newForm) {
        $("name").value = state.settings.name;
        $("backend-name").textContent = backendName(state.settings.backend);
        $("model-id").value = state.settings.model_id;
        $("codex-model").value = state.settings.model_id;
        $("reasoning-effort").value = state.settings.reasoning_effort || "medium";
        $("prompt").value = t("test_prompt");
        if (state.settings.groups) for (const key of ["files", "browser", "python"]) $(key).checked = state.settings.groups[key];
      }
      $("detail-title").textContent = t(state.page === "new" ? "add_provider" : "edit_provider");
      $("back").textContent = t(state.page === "new" ? "cancel" : "back");
      $("mlx-settings").hidden = state.settings.backend !== "mlx_lm";
      $("codex-settings").hidden = !cloud(state.settings.backend);
      $("tools").hidden = $("tools-help").hidden = cloud(state.settings.backend);
      $("auth-status").textContent = state.auth.message;
      $("auth-account").textContent = [state.auth.email, state.auth.plan_type].filter(Boolean).join(" · ");
      $("auth-account").hidden = !state.auth.signed_in || !$("auth-account").textContent;
      const chatgpt = state.settings.backend === "chatgpt";
      $("auth-status").dataset.kind = state.auth.phase === "error"
        || (chatgpt && state.auth.signed_in && !state.auth.ready && !authenticating(state.auth)) ? "error" : "info";
      $("login").textContent = t(authenticating(state.auth) ? "auth_cancel" : chatgpt ? "chatgpt_continue" : state.auth.signed_in ? "auth_relogin" : "auth_login");
      $("cloud-model-label").textContent = t(chatgpt ? "chatgpt_model" : "codex_model");
      $("cloud-model-help").textContent = t(chatgpt ? "chatgpt_model_help" : "codex_model_help");
      $("chatgpt-usage").hidden = !chatgpt;
      $("plan-state").textContent = t(state.auth.plan_enabled ? "chatgpt_using_plan" : "chatgpt_plan_help");
      $("models-status").textContent = state.models?.message || "";
      $("models-status").hidden = !chatgpt || !state.models?.message;
      if (chatgpt) $("codex-model").setAttribute("list", "chatgpt-models");
      else $("codex-model").removeAttribute("list");
      const catalog = JSON.stringify(chatgpt ? state.models?.items || [] : []);
      if (catalog !== modelsSignature) {
        modelsSignature = catalog;
        $("chatgpt-models").replaceChildren(...JSON.parse(catalog).map(model => {
          const option = document.createElement("option"); option.value = model.id; option.label = model.name; return option;
        }));
      }
      $("mlx-first-use").hidden = state.settings.backend !== "mlx_lm";
      $("availability").dataset.kind = state.availability.kind;
      $("availability-text").textContent = state.availability.message;
      $("install").firstElementChild.textContent = t(state.page === "new" ? "install" : "save_changes");
      $("test").textContent = t(state.test.running ? "cancel_test" : "test_start");
      $("test-output").hidden = !state.test.message && !state.test.text;
      $("test-status").textContent = state.test.message;
      $("reply").textContent = state.test.text;
      $("test-memory").textContent = state.test.memory;
      $("test-memory").hidden = !state.test.memory;
    }
    for (const id of ["install", "test", "new", "refresh", "back", "choose-back", "manage-usage", "plan-got-it", "name", "model-id", "tools", "prompt", "codex-model", "reasoning-effort", "login"]) {
      $(id).disabled = working || state.closed;
    }
    document.querySelectorAll(".provider-choice").forEach(button => { button.disabled = working || state.closed; });
    if (cloud(state.settings?.backend)) {
      $("install").disabled ||= authenticating(state.auth) || state.test.running || !state.auth.ready;
      $("test").disabled ||= authenticating(state.auth) || !state.auth.signed_in || (state.settings.backend === "chatgpt" && !state.auth.plan_enabled);
      $("login").disabled ||= state.test.running;
    }
    if (switched) { document.activeElement?.blur(); showError(null); window.scrollTo(0, 0); }
    document.documentElement.dataset.ready = "true";
    if (state.closed) stopped = true;
  }
  async function perform(action, payload) {
    if (working || stopped) return;
    working = true;
    showError(null);
    render(state);
    try { render(await call(action, payload)); }
    catch (error) { showError(error); }
    finally { working = false; render(state); }
  }
  $("new").addEventListener("click", () => perform("new"));
  $("refresh").addEventListener("click", () => perform("refresh"));
  document.querySelectorAll(".provider-choice").forEach(button => {
    button.addEventListener("click", () => perform("select_provider", { backend: button.dataset.backend }));
  });
  $("choose-back").addEventListener("click", () => perform("back"));
  $("plan-got-it").addEventListener("click", () => perform("acknowledge_plan"));
  $("manage-usage").addEventListener("click", () => perform("manage_usage"));
  $("install").addEventListener("click", () => perform("install", formPayload()));
  $("back").addEventListener("click", () => perform("back"));
  $("login").addEventListener("click", () => perform(authenticating(state.auth) ? "cancel_login" : "login",
    authenticating(state.auth) ? { editor_id: state.editor_id } : formPayload()));
  $("test").addEventListener("click", () => perform(state.test.running ? "cancel_test" : "test",
    state.test.running ? { editor_id: state.editor_id } : { ...formPayload(), prompt: $("prompt").value }));
  call("state").then(render).catch(showError);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state && !working && !stopped) perform("refresh"); });
  const poll = setInterval(async () => {
    if (stopped) { clearInterval(poll); return; }
    if (!state || polling || working) return;
    polling = true;
    try { render(await call("state")); }
    catch (error) { showError(error); }
    finally { polling = false; }
  }, 500);
})();
