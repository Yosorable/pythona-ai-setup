"""UI-independent settings actions and background model checks."""

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
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.worker = None
        self.availability = {"kind": "checking", "message": self.tr("checking")}
        self.test = {"running": False, "message": "", "text": ""}
        self.install_message = ""

    def snapshot(self):
        with self.lock:
            value = self.store.value
            return {"settings": {key: copy.deepcopy(value[key]) for key in ("name", "groups", "provider_id")},
                    "availability": dict(self.availability), "test": dict(self.test),
                    "install_message": self.install_message, "closed": self.closed.is_set()}

    def _settings(self, value):
        if not isinstance(value, dict) or set(value) - {"name", "groups"}:
            raise ValueError("Invalid settings request")
        settings = copy.deepcopy(self.store.value)
        settings.update(value)
        return validate(settings)

    def check_availability(self):
        def run():
            try:
                status = self.status()
                available = status["available"]
                value = {"kind": "available" if available else "unavailable",
                         "message": self.tr("available") if available else self.tr("unavailable", reason=self.tr.reason(status["reason"]))}
            except Exception as error:
                value = {"kind": "unavailable", "message": self.tr("check_failed", error=self.tr.error(error))}
            with self.lock:
                self.availability = value
        threading.Thread(target=run, name="Local LLM availability", daemon=True).start()

    def dispatch(self, action, payload=None):
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("The settings window has closed")
            if action == "state":
                return self.snapshot()
            if action in ("save", "install", "close"):
                settings = self._settings(payload)
                if action == "install":
                    provider_id = install(self.store, settings, self.ai)
                    self.install_message = self.tr("install_success", id=provider_id)
                else:
                    self.store.save(settings)
                if action == "close":
                    self.close()
                return self.snapshot()
            if action == "cancel_test":
                if self.test["running"]:
                    self.cancelled.set()
                    self.test["message"] = self.tr("cancelling")
                return self.snapshot()
            if action != "test" or not isinstance(payload, dict):
                raise ValueError("Unknown settings action")
            if self.test["running"]:
                raise RuntimeError("A model test is already running")
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
                raise ValueError("Enter a test message of up to 32,000 characters")
            self.store.save(self._settings(payload.get("settings")))
            settings = provider_settings(self.store.value)
            self.cancelled.clear()
            self.test = {"running": True, "message": self.tr("test_running"), "text": ""}

            def run():
                try:
                    result = self.probe(settings, prompt, self.cancelled)
                    message = self.tr("test_received", seconds=result["seconds"])
                    text = result["text"]
                except asyncio.CancelledError:
                    message, text = self.tr("test_cancelled"), ""
                except Exception as error:
                    message, text = self.tr("test_failed", error=self.tr.error(error)), ""
                with self.lock:
                    self.test = {"running": False, "message": message, "text": text}

            self.worker = threading.Thread(target=run, name="Local LLM test", daemon=True)
            self.worker.start()
            return self.snapshot()

    def close(self):
        self.closed.set()
        self.cancelled.set()
