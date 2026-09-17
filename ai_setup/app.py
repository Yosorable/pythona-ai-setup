"""Manage independent provider profiles and background model tests."""

import asyncio
import copy
import threading

from .bundle import load_backend
from .l10n import Localizer
from .probe import test_model
from .settings import install, provider_settings, validate


class SetupApp:
    def __init__(self, store, language="en", ai=None, status=None, probe=None):
        self.store = store
        self.tr = Localizer(language)
        self.ai = ai
        self.status = status or load_backend()["model_status"]
        self.probe = probe or test_model
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.availability = {}
        self.tests = {}
        self.jobs = {}
        self.install_messages = {}
        self.verified = {}
        self.revision = 0

    def snapshot(self):
        with self.lock:
            self.revision += 1
            value = self.store.value
            profiles = [{key: copy.deepcopy(profile[key]) for key in ("id", "name", "backend", "model_id", "provider_id")}
                        for profile in self.store.data["profiles"]]
            for profile in profiles:
                profile["installation_status"] = self.verified.get(profile["id"], "checking" if profile["provider_id"] else "not_installed")
            selected = self.store.data["selected_id"]
            return {"revision": self.revision, "profiles": profiles, "selected_id": selected,
                    "settings": None if value is None else {key: copy.deepcopy(value[key]) for key in
                                                            ("id", "name", "backend", "model_id", "groups", "provider_id")},
                    "availability": self.availability.get(selected, {"kind": "checking", "message": self.tr("checking")}),
                    "test": self.tests.get(selected, {"running": False, "message": "", "text": "", "memory": ""}),
                    "install_message": self.install_messages.get(selected, ""), "closed": self.closed.is_set()}

    def _settings(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"profile_id", "settings"}:
            raise ValueError("Invalid settings request")
        value = payload["settings"]
        if not isinstance(value, dict) or set(value) - {"name", "groups", "model_id"}:
            raise ValueError("Invalid settings request")
        settings = copy.deepcopy(self.store.get(payload["profile_id"]))
        settings.update(value)
        return validate(settings)

    def refresh_installations(self):
        """Only a definitive missing-ID response invalidates a saved installation."""
        with self.lock:
            profiles = list(self.store.data["profiles"])
            ai = self.ai
            changed = False
            for profile in profiles:
                profile_id, provider_id = profile["id"], profile["provider_id"]
                if not provider_id:
                    self.verified[profile_id] = "not_installed"
                    continue
                try:
                    if ai is None:
                        from pythona import ai
                    ai.get_custom_provider(provider_id)
                except KeyError:
                    profile["provider_id"] = None
                    self.verified[profile_id] = "not_installed"
                    self.install_messages[profile_id] = self.tr("provider_missing")
                    changed = True
                except Exception as error:
                    self.verified[profile_id] = "unknown"
                    self.install_messages[profile_id] = self.tr("provider_check_failed", error=self.tr.error(error))
                else:
                    self.verified[profile_id] = "installed"
                    self.install_messages.pop(profile_id, None)
            if changed:
                self.store.save()

    def check_availability(self, profile_id=None):
        with self.lock:
            profile_id = profile_id or self.store.data["selected_id"]
            if profile_id is None:
                return
            settings = provider_settings(self.store.get(profile_id), self.store.service)
            self.availability[profile_id] = {"kind": "checking", "message": self.tr("checking")}

        def run():
            try:
                status = self.status(settings)
                available = status["available"]
                message = self.tr("mlx_ready") if available and settings["backend"] == "mlx_lm" else self.tr("available")
                value = {"kind": "available" if available else "unavailable",
                         "message": message if available else self.tr("unavailable", reason=self.tr.reason(status["reason"]))}
            except Exception as error:
                value = {"kind": "unavailable", "message": self.tr("check_failed", error=self.tr.error(error))}
            with self.lock:
                if not self.closed.is_set():
                    self.availability[profile_id] = value
        threading.Thread(target=run, name="Local model availability", daemon=True).start()

    def dispatch(self, action, payload=None):
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("The settings window has closed")
            if action == "state":
                return self.snapshot()
            if action == "refresh":
                self.refresh_installations()
                self.check_availability()
                return self.snapshot()
            if action == "new":
                profile = self.store.add(payload["backend"])
                self.check_availability(profile["id"])
                return self.snapshot()
            if action == "select":
                self.store.select(payload["profile_id"])
                self.refresh_installations()
                self.check_availability()
                return self.snapshot()
            if action == "remove":
                profile_id = payload["profile_id"]
                self.refresh_installations()
                self.store.remove(profile_id)
                if profile_id in self.jobs:
                    self.jobs[profile_id]["cancelled"].set()
                self.check_availability()
                return self.snapshot()
            if action in ("save", "install", "close"):
                settings = None if action == "close" and payload is None and self.store.value is None else self._settings(payload)
                if action == "install":
                    provider_id = install(self.store, settings, self.ai)
                    self.verified[settings["id"]] = "installed"
                    self.install_messages[settings["id"]] = self.tr("install_success", id=provider_id)
                elif settings is not None:
                    self.store.save(settings)
                if action == "close":
                    self.close()
                return self.snapshot()
            if action == "cancel_test":
                profile_id = payload["profile_id"]
                job = self.jobs.get(profile_id)
                if job is not None and self.tests[profile_id]["running"]:
                    job["cancelled"].set()
                    self.tests[profile_id]["message"] = self.tr("cancelling")
                return self.snapshot()
            if action != "test" or not isinstance(payload, dict):
                raise ValueError("Unknown settings action")
            settings = self._settings({key: payload[key] for key in ("profile_id", "settings")})
            profile_id = settings["id"]
            if self.tests.get(profile_id, {}).get("running"):
                raise RuntimeError("A model test is already running")
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
                raise ValueError("Enter a test message of up to 32,000 characters")
            self.store.save(settings)
            runtime = provider_settings(settings, self.store.service)
            cancelled = threading.Event()
            self.tests[profile_id] = {"running": True, "message": self.tr("test_running"), "text": "", "memory": ""}

            def run():
                memory = ""
                try:
                    result = self.probe(runtime, prompt, cancelled)
                    message = self.tr("test_received", seconds=result["seconds"])
                    text = result["text"]
                    if result.get("memory"):
                        memory = self.tr("mlx_memory", **{key: value / 1_000_000 for key, value in result["memory"].items()})
                except asyncio.CancelledError:
                    message, text = self.tr("test_cancelled"), ""
                except Exception as error:
                    message, text = self.tr("test_failed", error=self.tr.error(error)), ""
                with self.lock:
                    self.tests[profile_id] = {"running": False, "message": message, "text": text, "memory": memory}

            worker = threading.Thread(target=run, name="Local model test", daemon=True)
            self.jobs[profile_id] = {"worker": worker, "cancelled": cancelled}
            worker.start()
            return self.snapshot()

    def close(self):
        self.closed.set()
        for job in list(self.jobs.values()):
            job["cancelled"].set()
