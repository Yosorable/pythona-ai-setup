(() => {
  "use strict";
  const { language, strings, preview, default_mlx_model } = window.SETUP;
  const $ = id => document.getElementById(id);
  const t = (key, values = {}) => strings[key].replace(/\{(\w+)(?::\.1f)?\}/g, (match, key) =>
    match.includes(":.1f") ? Number(values[key]).toFixed(1) : String(values[key]));
  document.documentElement.lang = language;
  document.title = "Pythona AI Setup";
  document.querySelectorAll("[data-i18n]").forEach(node => { node.textContent = t(node.dataset.i18n); });
  $("prompt").value = t("test_prompt");
  $("preview-note").hidden = !preview;
  let nextID = 0, state = null, working = false, stopped = false, polling = false;
  let writes = Promise.resolve();
  const pending = new Map(), prompts = new Map();
  const demoProfiles = [], demoTests = new Map(), demoTimers = new Map();
  let demoSelected = null, demoRevision = 0, demoClosed = false, demoNextID = 0, profilesSignature = "";
  function demoAdd(backend) {
    const profile = { id: "demo-" + (++demoNextID), name: backend === "apple_fm" ? "Apple On-Device Model" : "Qwen3-1.7B (MLX)",
      backend, model_id: backend === "mlx_lm" ? default_mlx_model : "", groups: { files: true, browser: false, python: false }, provider_id: null };
    demoProfiles.push(profile);
    demoSelected = profile.id;
  }
  if (preview) demoAdd("apple_fm");
  async function previewCall(action, payload) {
    const target = demoProfiles.find(profile => profile.id === payload?.profile_id);
    if (["save", "install", "test", "close"].includes(action) && target) {
      if (!payload.settings.name.trim()) throw new Error(t("empty_name"));
      if (target.backend === "mlx_lm" && !/^[\w.-]+\/[\w.-]+$/.test(payload.settings.model_id.trim())) throw new Error(t("invalid_model_id"));
      Object.assign(target, payload.settings);
    }
    if (action === "new") demoAdd(payload.backend);
    if (action === "select") demoSelected = target.id;
    if (action === "remove" && target && !target.provider_id) {
      clearTimeout(demoTimers.get(target.id));
      demoProfiles.splice(demoProfiles.indexOf(target), 1);
      demoSelected = demoProfiles[0]?.id || null;
    }
    if (action === "install") target.provider_id ||= "preview-" + target.id;
    if (action === "test") {
      if (!payload.prompt.trim()) throw new Error(t("your_message"));
      demoTests.set(target.id, { running: true, message: t("test_running"), text: "", memory: "" });
      demoTimers.set(target.id, setTimeout(() => {
        demoTests.set(target.id, { running: false, message: t("test_received", { seconds: 0.6 }),
          text: t("preview_note") + "\n\n" + payload.prompt, memory: "" });
      }, 600));
    }
    if (action === "cancel_test") {
      clearTimeout(demoTimers.get(target.id));
      demoTests.set(target.id, { running: false, message: t("test_cancelled"), text: "", memory: "" });
    }
    if (action === "close") { for (const timer of demoTimers.values()) clearTimeout(timer); demoClosed = true; }
    const selected = demoProfiles.find(profile => profile.id === demoSelected);
    return JSON.parse(JSON.stringify({ revision: ++demoRevision, selected_id: demoSelected,
      profiles: demoProfiles.map(profile => ({ ...profile, installation_status: profile.provider_id ? "installed" : "not_installed" })),
      settings: selected || null,
      availability: selected?.backend === "mlx_lm" ? { kind: "available", message: t("mlx_ready") }
        : { kind: "unavailable", message: t("unavailable", { reason: t("reason_DEVICE_NOT_ELIGIBLE") }) },
      test: demoTests.get(demoSelected) || { running: false, message: "", text: "", memory: "" },
      install_message: "", closed: demoClosed }));
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
    return { name: $("name").value, model_id: $("model-id").value,
      groups: Object.fromEntries(["files", "browser", "python"].map(key => [key, $(key).checked])) };
  }
  function formPayload() { return state?.settings ? { profile_id: state.selected_id, settings: formSettings() } : null; }
  function showError(error) {
    $("error").textContent = error ? String(error.message || error) : "";
    $("error").hidden = !error;
    if (error) $("error").scrollIntoView({ block: "nearest" });
  }
  function render(value) {
    if (!value || (state && value.revision < state.revision)) return;
    const switched = !state || state.selected_id !== value.selected_id;
    if (state?.selected_id) prompts.set(state.selected_id, $("prompt").value);
    state = value;
    const signature = JSON.stringify([state.profiles, state.selected_id, working, state.closed]);
    if (signature !== profilesSignature) {
      profilesSignature = signature;
      $("profiles").replaceChildren(...state.profiles.map(profile => {
      const button = document.createElement("button");
      button.className = "profile" + (profile.id === state.selected_id ? " selected" : "");
      button.dataset.id = profile.id;
      button.disabled = working || state.closed;
      const name = document.createElement("span"), detail = document.createElement("span");
      name.className = "profile-name"; detail.className = "profile-detail";
      name.textContent = profile.name;
      detail.textContent = (profile.backend === "apple_fm" ? t("apple_backend") : "MLX-LM · " + profile.model_id)
        + " · " + t("status_" + profile.installation_status);
      button.append(name, detail);
      button.addEventListener("click", () => changeProfile("select", { profile_id: profile.id }));
      return button;
      }));
    }
    $("editor").hidden = !state.settings;
    $("empty-profiles").hidden = state.profiles.length > 0;
    if (state.settings) {
      if (switched) {
        $("name").value = state.settings.name;
        $("model-id").value = state.settings.model_id;
        $("prompt").value = prompts.get(state.selected_id) || t("test_prompt");
        for (const key of ["files", "browser", "python"]) $(key).checked = state.settings.groups[key];
        showError(null);
      }
      $("mlx-settings").hidden = state.settings.backend !== "mlx_lm";
      $("availability").dataset.kind = state.availability.kind;
      $("availability-text").textContent = state.availability.message;
      $("install").firstElementChild.textContent = t(state.settings.provider_id ? "update" : "install");
      const profile = state.profiles.find(profile => profile.id === state.selected_id);
      $("installation").textContent = state.install_message || (profile.installation_status === "installed"
        ? t("installed", { id: state.settings.provider_id }) : t("status_" + profile.installation_status));
      $("test").textContent = t(state.test.running ? "cancel_test" : "test_start");
      $("test-output").hidden = !state.test.message && !state.test.text;
      $("test-status").textContent = state.test.message;
      $("reply").textContent = state.test.text;
      $("test-memory").textContent = state.test.memory;
      $("test-memory").hidden = !state.test.memory;
      $("remove").disabled = working || state.closed || Boolean(state.settings.provider_id);
      $("remove-help").hidden = !state.settings.provider_id;
    }
    for (const id of ["install", "test", "new", "refresh", "new-backend", "name", "model-id", "tools", "prompt"]) {
      $(id).disabled = working || state.closed;
    }
    document.documentElement.dataset.ready = "true";
    if (state.closed) stopped = true;
  }
  function mutate(action, payload) {
    const result = writes.catch(() => {}).then(() => call(action, payload));
    writes = result;
    return result;
  }
  async function perform(action, payload) {
    if (working || stopped) return;
    working = true;
    showError(null);
    render(state);
    try { render(await mutate(action, payload)); }
    catch (error) { showError(error); }
    finally { working = false; render(state); }
  }
  async function save() {
    if (!state?.settings || stopped || working) return;
    try { render(await mutate("save", formPayload())); showError(null); }
    catch (error) { showError(error); }
  }
  async function changeProfile(action, payload) {
    if (working || stopped) return;
    const current = formPayload();
    working = true;
    showError(null);
    render(state);
    try {
      if (current) render(await mutate("save", current));
      render(await mutate(action, payload));
    } catch (error) { showError(error); }
    finally { working = false; render(state); }
  }
  $("name").addEventListener("change", save);
  $("model-id").addEventListener("change", save);
  $("tools").addEventListener("change", save);
  $("install").addEventListener("click", () => perform("install", formPayload()));
  $("new").addEventListener("click", () => changeProfile("new", { backend: $("new-backend").value }));
  $("refresh").addEventListener("click", () => perform("refresh"));
  $("remove").addEventListener("click", () => perform("remove", { profile_id: state.selected_id }));
  window.setupBridge.close = async () => {
    await window.setupReady;
    await writes.catch(() => {});
    if (state && !stopped) await perform("close", formPayload());
  };
  $("test").addEventListener("click", () => perform(state.test.running ? "cancel_test" : "test",
    state.test.running ? { profile_id: state.selected_id } : { ...formPayload(), prompt: $("prompt").value }));
  window.setupReady = call("state").then(render).catch(showError);
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
