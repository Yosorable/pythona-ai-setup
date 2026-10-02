"""A provider list and transient add/edit forms with background model tests."""

import asyncio
import copy
import threading
import uuid

from .bundle import load_backend
from .codex_auth import AuthError, OAuthLogin
from .codex_probe import test_codex
from .chatgpt_auth import ChatGPTLogin, plan_enabled
from .chatgpt_probe import list_models, test_chatgpt
from .l10n import Localizer
from .probe import test_model
from .settings import BACKENDS, CLOUD_BACKENDS, authorization_error, defaults, install, provider_settings, read_credentials, same_account, validate, write_credentials


class SetupApp:
    def __init__(self, store, language="en", ai=None, status=None, probe=None, login_factory=OAuthLogin, codex_probe=None,
                 chatgpt_login_factory=ChatGPTLogin, chatgpt_probe=None, models_loader=list_models):
        self.store = store
        self.tr = Localizer(language)
        self.ai = ai
        self.status = status or load_backend()["model_status"]
        self.probe = probe or test_model
        self.codex_probe = codex_probe or test_codex
        self.login_factory = login_factory
        self.chatgpt_login_factory = chatgpt_login_factory
        self.chatgpt_probe = chatgpt_probe or test_chatgpt
        self.models_loader = models_loader
        self.models = {"items": [], "message": ""}
        self.model_generation = 0
        self.plan_notice = False
        self.login = None
        self.login_editor = None
        self.draft_auth = None
        self.account = {}
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.page = "home"
        self.editor_id = None
        self.draft = None
        self.availability = {}
        self.tests = {}
        self.jobs = {}
        self.cloud_operations = {}
        self.verified = {}
        self.notice = ""
        self.revision = 0

    def snapshot(self):
        with self.lock:
            self.poll_login()
            self.revision += 1
            profiles = [{key: copy.deepcopy(profile[key]) for key in ("id", "name", "backend", "model_id", "provider_id")}
                        for profile in self.store.data["profiles"] if profile["provider_id"]]
            for profile in profiles:
                profile["installation_status"] = self.verified.get(profile["id"], "checking")
            auth = {**self.account, **(self.login.snapshot() if self.login else
                    {"phase": "idle", "message": self._account_message()})}
            if auth["phase"] == "success" and not self.account.get("ready"):
                auth["message"] = self._account_message()
            return {"revision": self.revision, "page": self.page, "editor_id": self.editor_id, "profiles": profiles,
                    "settings": None if self.draft is None else {key: copy.deepcopy(self.draft[key]) for key in
                                                               ("id", "name", "backend", "model_id", "groups", "provider_id", "reasoning_effort")
                                                               if key in self.draft},
                    "auth": auth,
                    "models": copy.deepcopy(self.models), "plan_notice": self.plan_notice,
                    "availability": self.availability.get(self.editor_id, {"kind": "checking", "message": self.tr("checking")}),
                    "test": self.tests.get(self._running_test() or self.editor_id,
                                           {"running": False, "message": "", "text": "", "memory": ""}),
                    "notice": self.notice, "closed": self.closed.is_set()}

    def _settings(self, payload, validate_form=True):
        if not isinstance(payload, dict) or set(payload) != {"editor_id", "settings"}:
            raise ValueError("Invalid settings request")
        if self.draft is None or payload["editor_id"] != self.editor_id:
            raise ValueError("This form has closed; open the provider again")
        value = payload["settings"]
        if not isinstance(value, dict) or set(value) - {"name", "backend", "model_id", "groups", "reasoning_effort"} \
                or not {"name", "backend", "model_id"}.issubset(value):
            raise ValueError("Invalid settings request")
        if value["backend"] not in BACKENDS or not isinstance(value["name"], str) or not isinstance(value["model_id"], str):
            raise ValueError("Invalid settings request")
        if value["backend"] not in CLOUD_BACKENDS and (not isinstance(value.get("groups"), dict)
                or any(type(value["groups"].get(key)) is not bool for key in ("files", "browser", "python"))):
            raise ValueError("Invalid tool group settings")
        if value["backend"] != self.draft["backend"]:
            raise ValueError("The backend of an existing provider cannot be changed")
        settings = copy.deepcopy(self.draft)
        settings.update(value)
        return validate(settings) if validate_form else settings

    def _ai(self):
        if self.ai is not None:
            return self.ai
        from pythona import ai
        return ai

    def _set_account(self, auth):
        error = authorization_error(self.draft["backend"], auth)
        self.account = {"signed_in": error in (None, "chatgpt_plan_required"), "ready": error is None,
                        "email": auth.get("email", ""), "plan_type": auth.get("plan_type", ""),
                        "plan_enabled": plan_enabled(auth)}

    def _account_message(self):
        chatgpt = self.draft is not None and self.draft["backend"] == "chatgpt"
        if not self.account.get("signed_in"):
            return self.tr("chatgpt_auth_required" if chatgpt else "auth_required")
        if chatgpt:
            return self.tr("chatgpt_using_plan" if self.account.get("plan_enabled") else "chatgpt_plan_required")
        return self.tr("auth_signed_in")

    def poll_login(self):
        with self.lock:
            if self.login is not None and self.login_editor == self.editor_id and not self.closed.is_set():
                value = self.login.take_result()
                if value is not None:
                    self.draft_auth = value
                    self._set_account(value)
                    if self.draft["backend"] == "chatgpt":
                        self.plan_notice = plan_enabled(value) and value.get("plan_notice_seen") != "true"
                        self._load_models(value)

    def resume(self):
        with self.lock:
            if self.login is not None:
                self.login.resume()

    def _cancel_login(self):
        if self.login is not None:
            self.login.cancel()
        self.login = self.login_editor = None

    def _credentials(self):
        if self.draft_auth is not None:
            return dict(self.draft_auth)
        if self.draft["provider_id"]:
            storage = self._ai().get_custom_provider(self.draft["provider_id"])["local_storage"]
            return read_credentials(self.draft["backend"], storage)
        return {}

    def _cloud_operation(self, settings, cancelled=None):
        """Keep this setup window's catalog and tests sequential for one profile."""
        editor_id, draft_auth = self.editor_id, self.draft_auth
        backend, provider_id = settings["backend"], settings["provider_id"]
        operation_lock = self.cloud_operations.setdefault(settings["id"], threading.Lock())

        def save_refresh(previous, refreshed):
            with self.lock:
                if draft_auth is not None:
                    if self.editor_id == editor_id and self.draft_auth is draft_auth:
                        draft_auth.clear()
                        draft_auth.update(refreshed)
                        self._set_account(refreshed)
                    return refreshed
                ai = self._ai()
                storage = ai.get_custom_provider(provider_id)["local_storage"]
                current = read_credentials(backend, storage)
                if backend == "chatgpt" and not same_account(backend, previous, current):
                    raise AuthError("auth_changed")
                if current.get("refresh_token") != previous.get("refresh_token"):
                    if not same_account(backend, previous, current):
                        raise AuthError("auth_changed")
                    if current.get("refresh_token"):
                        if self.editor_id == editor_id and self.draft_auth is None:
                            self._set_account(current)
                        return current
                    # A failed concurrent refresh may have cleared tokens before
                    # this successful response arrived. Restore only that login.
                    if not (previous.get("local_grant_id") and refreshed.get("refresh_token")):
                        raise AuthError("auth_changed")
                if current.get("client_id") != previous.get("client_id"):
                    raise AuthError("auth_changed")
                write_credentials(backend, storage, refreshed)
                ai.update_custom_provider(provider_id, local_storage=storage)
                if self.editor_id == editor_id and self.draft_auth is None:
                    self._set_account(refreshed)
                return refreshed

        def run(operation):
            while not operation_lock.acquire(timeout=0.1):
                if self.closed.is_set() or self.editor_id != editor_id or (cancelled is not None and cancelled.is_set()):
                    raise asyncio.CancelledError
            try:
                with self.lock:
                    if self.closed.is_set() or self.editor_id != editor_id or (cancelled is not None and cancelled.is_set()):
                        raise asyncio.CancelledError
                    auth = dict(draft_auth) if draft_auth is not None else read_credentials(
                        backend, self._ai().get_custom_provider(provider_id)["local_storage"])
                return operation(auth, save_refresh)
            finally:
                operation_lock.release()
        return run

    def _load_models(self, auth):
        self.model_generation += 1
        generation, editor_id = self.model_generation, self.editor_id
        self.models = {"items": [], "message": ""}
        if not auth.get("access_token") or not plan_enabled(auth):
            return
        self.models["message"] = self.tr("chatgpt_models_loading")
        cloud_operation = self._cloud_operation(self.draft)

        def run():
            try:
                value = {"items": cloud_operation(self.models_loader), "message": ""}
            except asyncio.CancelledError:
                return
            except Exception:
                value = {"items": [], "message": self.tr("chatgpt_models_failed")}
            with self.lock:
                if not self.closed.is_set() and self.editor_id == editor_id and self.model_generation == generation:
                    self.models = value
        threading.Thread(target=run, name="ChatGPT model catalog", daemon=True).start()

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
                    if self.draft["backend"] in CLOUD_BACKENDS and self.draft_auth is None:
                        self._set_account({})
                    self.page = "new"
                    self.notice = self.tr("provider_missing")

    def check_availability(self):
        if self.draft is None:
            return
        if self.draft["backend"] in CLOUD_BACKENDS:
            self.availability[self.editor_id] = {"kind": "info", "message": self.tr(self.draft["backend"] + "_cloud")}
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

    def _running_test(self):
        if self.draft is not None:
            return next((editor for editor, job in self.jobs.items()
                         if job["profile_id"] == self.draft["id"] and self.tests[editor]["running"]), None)
        return None

    def _cancel_test(self):
        editor_id = self._running_test()
        if editor_id is not None:
            job = self.jobs[editor_id]
            job["cancelled"].set()
            self.tests[editor_id]["message"] = self.tr("cancelling")

    def _home(self):
        self._cancel_test()
        self._cancel_login()
        self.draft_auth, self.account = None, {}
        self.plan_notice = False
        self.models = {"items": [], "message": ""}
        self.page, self.editor_id, self.draft = "home", None, None

    def _edit(self, settings, page):
        self._cancel_test()
        self._cancel_login()
        self.draft_auth, self.account = None, {}
        self.plan_notice = False
        self.models = {"items": [], "message": ""}
        self.draft = copy.deepcopy(settings)
        self.page = page
        self.editor_id = uuid.uuid4().hex
        self.notice = ""
        if settings["backend"] in CLOUD_BACKENDS:
            try:
                auth = self._credentials()
                self._set_account(auth)
                if settings["backend"] == "chatgpt":
                    self._load_models(auth)
            except Exception as error:
                self.notice = self.tr("auth_lookup_failed", error=self.tr.error(error))
        self.check_availability()

    def dispatch(self, action, payload=None):
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("The settings window has closed")
            if action != "cancel_login":
                self.poll_login()
            if action == "state":
                return self.snapshot()
            if action == "close":
                self.close()
                return self.snapshot()
            if action == "refresh":
                self.notice = ""
                self.refresh_installations()
                self.resume()
                return self.snapshot()
            if action == "new":
                self._home()
                self.notice = ""
                self.page = "choose"
                return self.snapshot()
            if action == "select_provider":
                if self.page != "choose" or not isinstance(payload, dict) or set(payload) != {"backend"} or payload["backend"] not in BACKENDS:
                    raise ValueError("Choose a provider first")
                self._edit(defaults(payload["backend"]), "new")
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
            if action in ("acknowledge_plan", "manage_usage"):
                if self.draft is None or self.draft["backend"] != "chatgpt":
                    raise ValueError("Open a ChatGPT provider first")
                if action == "acknowledge_plan":
                    self.plan_notice = False
                    if self.draft_auth is not None:
                        self.draft_auth["plan_notice_seen"] = "true"
                return self.snapshot()
            if action == "install":
                settings = self._settings(payload)
                if self.login is not None and self.login.snapshot()["phase"] in ("starting", "waiting", "exchanging"):
                    raise AuthError("auth_in_progress")
                if settings["backend"] in CLOUD_BACKENDS and self._running_test():
                    raise AuthError("auth_test_running")
                added = self.page == "new"
                try:
                    install(self.store, settings, self.ai, auth=self.draft_auth)
                except AuthError as error:
                    # Reconcile cached UI state if provider storage changed since
                    # the editor opened, without discarding a draft or known ID.
                    if self.draft_auth is not None:
                        self._set_account(self.draft_auth)
                    elif str(error) == "chatgpt_plan_required":
                        self.account.update(ready=False, plan_enabled=False)
                    else:
                        self._set_account({})
                    raise
                self.verified[settings["id"]] = "installed"
                self._home()
                self.notice = self.tr("provider_added" if added else "provider_updated")
                return self.snapshot()
            if action == "login":
                settings = self._settings(payload, validate_form=False)
                if settings["backend"] not in CLOUD_BACKENDS:
                    raise ValueError("Sign-in requires a cloud provider")
                if self._running_test():
                    raise AuthError("auth_test_running")
                previous = self._credentials()
                if settings["backend"] == "chatgpt" and self.login is not None and not previous.get("client_id"):
                    previous = dict(getattr(self.login, "registration", {}))
                self._cancel_login()
                self.draft = settings
                self.login = (self.chatgpt_login_factory(self.tr, self.store.ensure_chatgpt_host_id(), previous)
                              if settings["backend"] == "chatgpt" else self.login_factory(self.tr))
                self.login_editor = self.editor_id
                try:
                    self.login.start()
                except Exception:
                    self._cancel_login()
                    raise
                return self.snapshot()
            if action == "cancel_login":
                if self.draft is None or payload.get("editor_id") != self.editor_id:
                    raise ValueError("This form has closed; open the provider again")
                if self.login is not None:
                    self.login.cancel()
                return self.snapshot()
            if action == "cancel_test":
                if self.draft is None or payload.get("editor_id") != self.editor_id:
                    raise ValueError("This form has closed; open the provider again")
                self._cancel_test()
                return self.snapshot()
            if action != "test" or not isinstance(payload, dict):
                raise ValueError("Unknown settings action")
            settings = self._settings({key: payload[key] for key in ("editor_id", "settings")})
            if self.login is not None and self.login.snapshot()["phase"] in ("starting", "waiting", "exchanging"):
                raise AuthError("auth_in_progress")
            editor_id = self.editor_id
            if self._running_test():
                raise RuntimeError("A model test is already running")
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
                raise ValueError("Enter a test message of up to 32,000 characters")
            self.draft = settings
            runtime = provider_settings(settings, self.store.service)
            auth = self._credentials() if settings["backend"] in CLOUD_BACKENDS else None
            if auth is not None and not auth.get("refresh_token"):
                raise AuthError("chatgpt_auth_required" if settings["backend"] == "chatgpt" else "auth_required")
            if settings["backend"] == "chatgpt" and not plan_enabled(auth):
                raise AuthError("chatgpt_plan_required")
            cancelled = threading.Event()
            cloud_operation = self._cloud_operation(settings, cancelled) if auth is not None else None
            self.tests[editor_id] = {"running": True, "message": self.tr("test_running"), "text": "", "memory": ""}

            def run():
                memory = ""
                try:
                    cloud_probe = self.chatgpt_probe if settings["backend"] == "chatgpt" else self.codex_probe
                    result = (cloud_operation(lambda latest, save: cloud_probe(runtime, prompt, cancelled, latest, save)) if auth is not None
                              else self.probe(runtime, prompt, cancelled))
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
            self.jobs[editor_id] = {"worker": worker, "cancelled": cancelled, "profile_id": settings["id"]}
            worker.start()
            return self.snapshot()

    def close(self):
        self.closed.set()
        self._cancel_login()
        self.draft_auth = None
        for job in list(self.jobs.values()):
            job["cancelled"].set()
