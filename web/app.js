(() => {
  "use strict";
  const { language, strings, preview, default_mlx_model } = window.SETUP;
  const $ = id => document.getElementById(id);
  const t = (key, values = {}) => strings[key].replace(/\{(\w+)(?::\.1f)?\}/g, (match, key) =>
    match.includes(":.1f") ? Number(values[key]).toFixed(1) : String(values[key]));
  document.documentElement.lang = language;
  document.title = "Pythona AI Setup";
  document.querySelectorAll("[data-i18n]").forEach(node => { node.textContent = t(node.dataset.i18n); });
  $("preview-note").hidden = !preview;
  let nextID = 0, state = null, working = false, stopped = false, polling = false, profilesSignature = "";
  const pending = new Map();
  const demo = { revision: 0, page: "home", editor_id: null, profiles: [], settings: null,
    availability: {}, test: {}, notice: "", closed: false };
  let demoID = 0, demoTimer = null;
  const defaultName = backend => backend === "apple_fm" ? "Apple On-Device Model" : "Qwen3-1.7B (MLX)";
  function demoOpen(settings, page) {
    clearTimeout(demoTimer);
    demo.page = page;
    demo.settings = JSON.parse(JSON.stringify(settings));
    demo.editor_id = "editor-" + (++demoID);
    demo.notice = "";
    demo.test = { running: false, message: "", text: "", memory: "" };
    demo.availability = settings.backend === "mlx_lm" ? { kind: "available", message: t("mlx_ready") }
      : { kind: "unavailable", message: t("unavailable", { reason: t("reason_DEVICE_NOT_ELIGIBLE") }) };
  }
  function demoHome() {
    clearTimeout(demoTimer);
    demo.page = "home"; demo.settings = null; demo.editor_id = null;
  }
  async function previewCall(action, payload) {
    if (["backend", "install", "test"].includes(action)) {
      if (payload.editor_id !== demo.editor_id) throw new Error("This form has closed.");
      if (action !== "backend") {
        if (!payload.settings.name.trim()) throw new Error(t("empty_name"));
        if (payload.settings.backend === "mlx_lm" && !/^[\w.-]+\/[\w.-]+$/.test(payload.settings.model_id.trim())) throw new Error(t("invalid_model_id"));
      }
    }
    if (action === "new") demoOpen({ id: "profile-" + (++demoID), name: defaultName("apple_fm"), backend: "apple_fm", model_id: "",
      groups: { files: true, browser: false, python: false }, provider_id: null }, "new");
    if (action === "edit") demoOpen(demo.profiles.find(profile => profile.id === payload.profile_id), "edit");
    if (action === "backend") {
      const settings = { ...demo.settings, ...payload.settings };
      if (settings.name === defaultName(demo.settings.backend)) settings.name = defaultName(settings.backend);
      settings.model_id = settings.backend === "mlx_lm" ? default_mlx_model : "";
      demoOpen(settings, "new");
    }
    if (action === "back") { demoHome(); demo.notice = ""; }
    if (action === "install") {
      const added = demo.page === "new";
      const profile = { ...demo.settings, ...payload.settings, installation_status: "installed" };
      profile.provider_id ||= "preview-" + profile.id;
      const index = demo.profiles.findIndex(value => value.id === profile.id);
      if (index < 0) demo.profiles.push(profile); else demo.profiles[index] = profile;
      demoHome();
      demo.notice = t(added ? "provider_added" : "provider_updated");
    }
    if (action === "test") {
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
    return { editor_id: state.editor_id, settings: { name: $("name").value, backend: $("backend").value,
      model_id: $("backend").value === "mlx_lm" ? $("model-id").value : "",
      groups: Object.fromEntries(["files", "browser", "python"].map(key => [key, $(key).checked])) } };
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
    $("detail").hidden = state.page === "home";
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
        detail.textContent = profile.backend === "apple_fm" ? t("apple_backend") : "MLX-LM · " + profile.model_id;
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
        $("backend").value = state.settings.backend;
        $("model-id").value = state.settings.model_id;
        $("prompt").value = t("test_prompt");
        for (const key of ["files", "browser", "python"]) $(key).checked = state.settings.groups[key];
      }
      $("detail-title").textContent = t(state.page === "new" ? "add_provider" : "edit_provider");
      $("back").textContent = t(state.page === "new" ? "cancel" : "back");
      $("mlx-settings").hidden = state.settings.backend !== "mlx_lm";
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
    for (const id of ["install", "test", "new", "refresh", "back", "backend", "name", "model-id", "tools", "prompt"]) {
      $(id).disabled = working || state.closed;
    }
    $("backend").disabled ||= state.page === "edit";
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
  $("backend").addEventListener("change", () => perform("backend", formPayload()));
  $("install").addEventListener("click", () => perform("install", formPayload()));
  $("back").addEventListener("click", () => perform("back"));
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
