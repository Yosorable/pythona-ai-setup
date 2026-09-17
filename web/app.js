(() => {
  "use strict";
  const { language, strings, preview } = window.SETUP;
  const $ = id => document.getElementById(id);
  const t = (key, values = {}) => strings[key].replace(/\{(\w+)(?::\.1f)?\}/g, (match, key) =>
    match.includes(":.1f") ? Number(values[key]).toFixed(1) : String(values[key]));
  document.documentElement.lang = language;
  document.title = t("title");
  document.querySelectorAll("[data-i18n]").forEach(node => { node.textContent = t(node.dataset.i18n); });
  $("prompt").value = t("test_prompt");
  $("preview-note").hidden = !preview;
  let nextID = 0, state = null, working = false, stopped = false, polling = false;
  let writes = Promise.resolve();
  const pending = new Map();
  const demo = {
    settings: { name: "Apple On-Device Model", groups: { files: true, browser: false, python: false }, provider_id: null },
    availability: { kind: "unavailable", message: t("unavailable", { reason: t("reason_DEVICE_NOT_ELIGIBLE") }) },
    test: { running: false, message: "", text: "" }, install_message: "", closed: false,
  };
  let demoTest = null;

  async function previewCall(action, payload) {
    if (payload?.name !== undefined && !payload.name.trim()) throw new Error(t("empty_name"));
    if (["save", "install", "close"].includes(action)) Object.assign(demo.settings, payload);
    if (action === "install") {
      demo.settings.provider_id = "preview-provider";
      demo.install_message = t("install_success", { id: demo.settings.provider_id });
    }
    if (action === "test") {
      if (!payload.prompt.trim()) throw new Error(t("your_message"));
      Object.assign(demo.settings, payload.settings);
      demo.test = { running: true, message: t("test_running"), text: "" };
      demoTest = setTimeout(() => {
        demo.test = { running: false, message: t("test_received", { seconds: 0.6 }), text: t("preview_note") + "\n\n" + payload.prompt };
      }, 600);
    }
    if (action === "cancel_test") {
      clearTimeout(demoTest);
      demo.test = { running: false, message: t("test_cancelled"), text: "" };
    }
    if (action === "close") { clearTimeout(demoTest); demo.closed = true; }
    return JSON.parse(JSON.stringify(demo));
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

  function formSettings() {
    return { name: $("name").value, groups: Object.fromEntries(["files", "browser", "python"].map(key => [key, $(key).checked])) };
  }
  function showError(error) {
    $("error").textContent = error ? String(error.message || error) : "";
    $("error").hidden = !error;
  }
  function render(value) {
    const initial = state === null;
    state = value;
    if (initial) {
      $("name").value = state.settings.name;
      for (const key of ["files", "browser", "python"]) $(key).checked = state.settings.groups[key];
      $("name").disabled = $("tools").disabled = $("prompt").disabled = false;
      document.documentElement.dataset.ready = "true";
    }
    $("availability").dataset.kind = state.availability.kind;
    $("availability-text").textContent = state.availability.message;
    $("install").firstElementChild.textContent = t(state.settings.provider_id ? "update" : "install");
    $("installation").textContent = state.install_message || (state.settings.provider_id ? t("installed", { id: state.settings.provider_id }) : t("not_installed"));
    $("test").textContent = t(state.test.running ? "cancel_test" : "test_start");
    $("test-output").hidden = !state.test.message && !state.test.text;
    $("test-status").textContent = state.test.message;
    $("reply").textContent = state.test.text;
    for (const id of ["install", "test"]) $(id).disabled = working || state.closed;
    if (state.closed) stopped = true;
  }
  function mutate(action, payload) {
    const result = writes.catch(() => {}).then(() => call(action, payload));
    writes = result;
    return result;
  }
  async function perform(action, payload) {
    if (working) return;
    working = true;
    showError(null);
    render(state);
    try { render(await mutate(action, payload)); }
    catch (error) { showError(error); }
    finally { working = false; render(state); }
  }
  async function save() {
    if (!state || stopped) return;
    try { render(await mutate("save", formSettings())); showError(null); }
    catch (error) { showError(error); }
  }
  $("name").addEventListener("change", save);
  $("tools").addEventListener("change", save);
  $("install").addEventListener("click", () => perform("install", formSettings()));
  window.setupBridge.close = async () => {
    await window.setupReady;
    await writes.catch(() => {});
    if (state && !stopped) await perform("close", formSettings());
  };
  $("test").addEventListener("click", () => perform(state.test.running ? "cancel_test" : "test",
    state.test.running ? null : { settings: formSettings(), prompt: $("prompt").value }));
  window.setupReady = call("state").then(render).catch(showError);
  const poll = setInterval(async () => {
    if (stopped) { clearInterval(poll); return; }
    if (!state || polling || working) return;
    polling = true;
    try { render(await call("state")); }
    catch (error) { showError(error); }
    finally { polling = false; }
  }, 500);
})();
