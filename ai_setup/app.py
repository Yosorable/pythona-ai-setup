"""A provider list and transient add/edit forms with background model tests."""

import asyncio
import copy
import threading
import uuid

from .bundle import load_backend
from .l10n import Localizer
from .probe import test_model
from .settings import BACKENDS, defaults, install, provider_settings, validate


class SetupApp:
    def __init__(self, store, language="en", ai=None, status=None, probe=None):
        self.store = store
        self.tr = Localizer(language)
        self.ai = ai
        self.status = status or load_backend()["model_status"]
        self.probe = probe or test_model
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.page = "home"
        self.editor_id = None
        self.draft = None
        self.availability = {}
        self.tests = {}
        self.jobs = {}
        self.verified = {}
        self.notice = ""
        self.revision = 0

    def snapshot(self):
        with self.lock:
            self.revision += 1
            profiles = [{key: copy.deepcopy(profile[key]) for key in ("id", "name", "backend", "model_id", "provider_id")}
                        for profile in self.store.data["profiles"] if profile["provider_id"]]
            for profile in profiles:
                profile["installation_status"] = self.verified.get(profile["id"], "checking")
            return {"revision": self.revision, "page": self.page, "editor_id": self.editor_id, "profiles": profiles,
                    "settings": None if self.draft is None else {key: copy.deepcopy(self.draft[key]) for key in
                                                               ("id", "name", "backend", "model_id", "groups", "provider_id")},
                    "availability": self.availability.get(self.editor_id, {"kind": "checking", "message": self.tr("checking")}),
                    "test": self.tests.get(self.editor_id, {"running": False, "message": "", "text": "", "memory": ""}),
                    "notice": self.notice, "closed": self.closed.is_set()}

    def _settings(self, payload, validate_form=True):
        if not isinstance(payload, dict) or set(payload) != {"editor_id", "settings"}:
            raise ValueError("Invalid settings request")
        if self.draft is None or payload["editor_id"] != self.editor_id:
            raise ValueError("This form has closed; open the provider again")
        value = payload["settings"]
        if not isinstance(value, dict) or set(value) != {"name", "backend", "model_id", "groups"}:
            raise ValueError("Invalid settings request")
        if value["backend"] not in BACKENDS or not isinstance(value["name"], str) or not isinstance(value["model_id"], str):
            raise ValueError("Invalid settings request")
        if not isinstance(value["groups"], dict) or any(type(value["groups"].get(key)) is not bool for key in ("files", "browser", "python")):
            raise ValueError("Invalid tool group settings")
        if self.page == "edit" and value["backend"] != self.draft["backend"]:
            raise ValueError("The backend of an existing provider cannot be changed")
        settings = copy.deepcopy(self.draft)
        settings.update(value)
        return validate(settings) if validate_form else settings

    def refresh_installations(self):
        """Only confirmed missing IDs remove entries; lookup errors preserve their records."""
        with self.lock:
            ai = self.ai
            removed = []
            for profile in self.store.data["profiles"]:
                profile_id, provider_id = profile["id"], profile["provider_id"]
                if not provider_id:
                    removed.append(profile_id)
                    continue
                try:
                    if ai is None:
                        from pythona import ai
                    ai.get_custom_provider(provider_id)
                except KeyError:
                    removed.append(profile_id)
                except Exception:
                    self.verified[profile_id] = "unknown"
                else:
                    self.verified[profile_id] = "installed"
            if removed:
                self.store.remove(removed)
                for profile_id in removed:
                    self.verified.pop(profile_id, None)
                if self.draft is not None and self.draft["id"] in removed:
                    # Keep an open form available for reinstalling its deleted provider.
                    self.draft["provider_id"] = None
                    self.page = "new"
                    self.notice = self.tr("provider_missing")

    def check_availability(self):
        if self.draft is None:
            return
        editor_id = self.editor_id
        settings = {"backend": self.draft["backend"], "model_id": self.draft["model_id"]}
        self.availability[editor_id] = {"kind": "checking", "message": self.tr("checking")}

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
                if not self.closed.is_set() and self.editor_id == editor_id:
                    self.availability[editor_id] = value
        threading.Thread(target=run, name="Local model availability", daemon=True).start()

    def _cancel_test(self):
        job = self.jobs.get(self.editor_id)
        if job is not None and self.tests[self.editor_id]["running"]:
            job["cancelled"].set()
            self.tests[self.editor_id]["message"] = self.tr("cancelling")

    def _home(self):
        self._cancel_test()
        self.page, self.editor_id, self.draft = "home", None, None

    def _edit(self, settings, page):
        self._cancel_test()
        self.draft = copy.deepcopy(settings)
        self.page = page
        self.editor_id = uuid.uuid4().hex
        self.notice = ""
        self.check_availability()

    def dispatch(self, action, payload=None):
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("The settings window has closed")
            if action == "state":
                return self.snapshot()
            if action == "close":
                self.close()
                return self.snapshot()
            if action == "refresh":
                self.notice = ""
                self.refresh_installations()
                return self.snapshot()
            if action == "new":
                self._edit(defaults(), "new")
                return self.snapshot()
            if action == "edit":
                self.refresh_installations()
                try:
                    profile = self.store.get(payload["profile_id"])
                except KeyError:
                    self._home()
                    self.notice = self.tr("provider_removed")
                else:
                    self._edit(profile, "edit")
                return self.snapshot()
            if action == "back":
                self._home()
                self.notice = ""
                self.refresh_installations()
                return self.snapshot()
            if action == "backend":
                if self.page != "new":
                    raise ValueError("The backend of an existing provider cannot be changed")
                settings = self._settings(payload, validate_form=False)
                old = defaults(self.draft["backend"])
                new = defaults(settings["backend"])
                if settings["name"] == old["name"]:
                    settings["name"] = new["name"]
                settings["model_id"] = new["model_id"]
                self._edit(settings, "new")
                return self.snapshot()
            if action == "install":
                settings = self._settings(payload)
                added = self.page == "new"
                install(self.store, settings, self.ai)
                self.verified[settings["id"]] = "installed"
                self._home()
                self.notice = self.tr("provider_added" if added else "provider_updated")
                return self.snapshot()
            if action == "cancel_test":
                if self.draft is None or payload.get("editor_id") != self.editor_id:
                    raise ValueError("This form has closed; open the provider again")
                self._cancel_test()
                return self.snapshot()
            if action != "test" or not isinstance(payload, dict):
                raise ValueError("Unknown settings action")
            settings = self._settings({key: payload[key] for key in ("editor_id", "settings")})
            editor_id = self.editor_id
            if self.tests.get(editor_id, {}).get("running"):
                raise RuntimeError("A model test is already running")
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
                raise ValueError("Enter a test message of up to 32,000 characters")
            self.draft = settings
            runtime = provider_settings(settings, self.store.service)
            cancelled = threading.Event()
            self.tests[editor_id] = {"running": True, "message": self.tr("test_running"), "text": "", "memory": ""}

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
                    self.tests[editor_id] = {"running": False, "message": message, "text": text, "memory": memory}

            worker = threading.Thread(target=run, name="Local model test", daemon=True)
            self.jobs[editor_id] = {"worker": worker, "cancelled": cancelled}
            worker.start()
            return self.snapshot()

    def close(self):
        self.closed.set()
        for job in list(self.jobs.values()):
            job["cancelled"].set()
