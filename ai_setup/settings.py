"""Persist independent provider profiles and their shared local service settings."""

import copy
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import uuid


STORAGE_KEY = "local_model_settings"
CODEX_STORAGE_KEY = "codex_settings"
CHATGPT_STORAGE_KEY = "chatgpt_settings"
AUTH_KEYS = ("access_token", "refresh_token", "id_token", "account_id", "email", "plan_type", "last_refresh")
STATE_PATH = Path(__file__).resolve().parents[1] / "settings.local.json"
DEFAULT_MLX_MODEL = "mlx-community/Qwen3-1.7B-4bit"
DEFAULT_CODEX_MODEL = "gpt-6.1-sol"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
BACKENDS = ("apple_fm", "mlx_lm", "codex", "chatgpt")
CLOUD_BACKENDS = ("codex", "chatgpt")


def authorization_error(backend, auth):
    """Require a completed account setup, independently of model test results."""
    if backend not in CLOUD_BACKENDS:
        return None
    required = "chatgpt_auth_required" if backend == "chatgpt" else "auth_required"
    if not isinstance(auth, dict) or any(not isinstance(auth.get(key), str) or not auth[key].strip()
                                         for key in ("access_token", "refresh_token")):
        return required
    if backend == "chatgpt":
        from .chatgpt_auth import ISSUER, plan_enabled
        if (not isinstance(auth.get("client_id"), str) or not re.fullmatch(r"oaiapp_[A-Za-z0-9_-]{1,200}", auth["client_id"])
                or not isinstance(auth.get("subject"), str) or not auth["subject"].strip() or auth.get("issuer") != ISSUER):
            return required
        if not plan_enabled(auth):
            return "chatgpt_plan_required"
    return None


def read_credentials(backend, storage):
    if backend == "chatgpt":
        from .chatgpt_auth import AUTH_FIELDS, AUTH_STORAGE_KEY
        value = json.loads(storage.get(AUTH_STORAGE_KEY, "{}"))
        if not isinstance(value, dict) or any(key not in AUTH_FIELDS or not isinstance(item, str) for key, item in value.items()):
            raise ValueError("Invalid account credentials")
        return value
    return {key: storage[key] for key in AUTH_KEYS if key in storage}


def same_account(backend, first, second):
    if backend == "chatgpt":
        return all(first.get(key) == second.get(key) for key in ("client_id", "subject", "issuer", "local_grant_id"))
    return bool(first.get("account_id")) and first["account_id"] == second.get("account_id")


def write_credentials(backend, storage, auth):
    from .chatgpt_auth import AUTH_FIELDS, AUTH_STORAGE_KEY
    keys = AUTH_FIELDS if backend == "chatgpt" else AUTH_KEYS
    if not isinstance(auth, dict) or any(key not in keys or not isinstance(item, str) for key, item in auth.items()):
        raise ValueError("Invalid account credentials")
    if backend == "chatgpt":
        # One provider-storage value replaces a rotating credential set atomically.
        storage[AUTH_STORAGE_KEY] = json.dumps(auth, ensure_ascii=False)
    else:
        for key in AUTH_KEYS:
            storage.pop(key, None)
        storage.update(auth)


def service_defaults():
    return {"port": 8768, "idle_seconds": 300, "request_seconds": 120, "load_seconds": 900,
            "service_token": secrets.token_urlsafe(24)}


def defaults(backend="apple_fm"):
    if backend not in BACKENDS:
        raise ValueError("Unknown model backend")
    value = {"id": uuid.uuid4().hex, "backend": backend,
            "name": {"apple_fm": "Apple On-Device Model", "mlx_lm": "Qwen3-1.7B (MLX)", "codex": "Codex", "chatgpt": "ChatGPT"}[backend],
            "model_id": {"apple_fm": "", "mlx_lm": DEFAULT_MLX_MODEL, "codex": DEFAULT_CODEX_MODEL, "chatgpt": DEFAULT_CODEX_MODEL}[backend],
            "provider_id": None}
    if backend in CLOUD_BACKENDS:
        return {**value, "reasoning_effort": "medium"}
    return {**value, "groups": {"files": True, "browser": False, "python": False}, "maximum_response_tokens": 800}


def validate(settings):
    value = copy.deepcopy(settings)
    if not isinstance(value, dict) or value.get("backend") not in BACKENDS:
        raise ValueError("Unknown model backend")
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise ValueError("Invalid profile ID")
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise ValueError("Connection name must not be empty")
    value["name"] = value["name"].strip()
    if value["backend"] not in CLOUD_BACKENDS:
        if not isinstance(value.get("groups"), dict):
            raise ValueError("Invalid tool group settings")
        for group in ("files", "browser", "python"):
            if type(value["groups"].get(group)) is not bool:
                raise ValueError(f"Invalid tool group setting: {group}")
        if type(value.get("maximum_response_tokens")) is not int or not 1 <= value["maximum_response_tokens"] <= 8192:
            raise ValueError("Invalid maximum_response_tokens")
    if not isinstance(value.get("model_id"), str):
        raise ValueError("Enter a Hugging Face model ID")
    value["model_id"] = value["model_id"].strip()
    if value["backend"] == "mlx_lm" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value["model_id"]):
        raise ValueError("Enter a Hugging Face model ID")
    if value["backend"] == "apple_fm" and value["model_id"]:
        raise ValueError("Apple Foundation Models does not accept a model ID")
    if value["backend"] in CLOUD_BACKENDS:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value["model_id"]):
            raise ValueError("Enter a Codex model ID" if value["backend"] == "codex" else "chatgpt_invalid_model")
        if value.get("reasoning_effort") not in REASONING_EFFORTS:
            raise ValueError("Invalid reasoning effort")
        value.pop("groups", None)
        value.pop("maximum_response_tokens", None)
    from .chatgpt_auth import AUTH_FIELDS, AUTH_STORAGE_KEY
    if any(key in value for key in (*AUTH_KEYS, *AUTH_FIELDS, AUTH_STORAGE_KEY)):
        raise ValueError("Account credentials belong in provider storage, not settings")
    if value.get("provider_id") is not None and (not isinstance(value["provider_id"], str) or not value["provider_id"]):
        raise ValueError("Invalid saved provider ID")
    return value


def validate_service(service):
    value = copy.deepcopy(service)
    for key, minimum, maximum in (("port", 1024, 65535), ("idle_seconds", 5, 86400),
                                  ("request_seconds", 1, 3600), ("load_seconds", 1, 3600)):
        if type(value.get(key)) is not int or not minimum <= value[key] <= maximum:
            raise ValueError(f"Setting out of range: {key}")
    if not isinstance(value.get("service_token"), str) or len(value["service_token"]) < 16:
        raise ValueError("Invalid local service credentials")
    return value


def provider_settings(settings, service):
    if settings["backend"] in CLOUD_BACKENDS:
        value = validate(settings)
        return {key: value[key] for key in ("backend", "model_id", "reasoning_effort")}
    return {**validate_service(service), **{key: value for key, value in validate(settings).items()
            if key not in ("id", "name", "provider_id")}}


def validate_host_id(value):
    if not isinstance(value, str) or not value.startswith("urn:uuid:"):
        raise ValueError("Invalid ChatGPT installation identifier")
    parsed = uuid.UUID(value[9:])
    if parsed.version != 4 or str(parsed) != value[9:]:
        raise ValueError("Invalid ChatGPT installation identifier")
    return value


class SettingsStore:
    def __init__(self, path=STATE_PATH):
        self.path = Path(path)
        self.data = {"service": service_defaults(), "profiles": []}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError) as error:
            raise RuntimeError(f"Could not read settings; the original file was preserved: {self.path}\n{error}") from error
        self.data = self._validate(saved)

    @staticmethod
    def _validate(data):
        if (not isinstance(data, dict) or not {"service", "profiles"} <= data.keys()
                or data.keys() - {"service", "profiles", "chatgpt_host_id"}):
            raise ValueError("Invalid settings file structure")
        if not isinstance(data["profiles"], list):
            raise ValueError("Profiles must be an array")
        profiles = [validate(value) for value in data["profiles"]]
        if any(not value["provider_id"] for value in profiles):
            raise ValueError("Saved profiles must have an installed provider ID")
        ids = [value["id"] for value in profiles]
        installed = [value["provider_id"] for value in profiles]
        if len(ids) != len(set(ids)) or len(installed) != len(set(installed)):
            raise ValueError("Duplicate profile or provider IDs")
        value = {"service": validate_service(data["service"]), "profiles": profiles}
        if "chatgpt_host_id" in data:
            value["chatgpt_host_id"] = validate_host_id(data["chatgpt_host_id"])
        return value

    def ensure_chatgpt_host_id(self):
        if "chatgpt_host_id" not in self.data:
            self.data["chatgpt_host_id"] = "urn:uuid:" + str(uuid.uuid4())
            try:
                self.save()
            except OSError:
                self.data.pop("chatgpt_host_id", None)
                raise
        return self.data["chatgpt_host_id"]

    @property
    def service(self):
        return self.data["service"]

    def get(self, profile_id):
        for profile in self.data["profiles"]:
            if profile["id"] == profile_id:
                return profile
        raise KeyError("The configuration no longer exists")

    def remove(self, profile_ids):
        self.data["profiles"] = [value for value in self.data["profiles"] if value["id"] not in profile_ids]
        self.save()

    def save(self, value=None):
        # Retain installed IDs in memory if writing the local record fails.
        if value is not None:
            value = validate(value)
            profiles = self.data["profiles"]
            if any(profile["id"] == value["id"] for profile in profiles):
                self.data["profiles"] = [value if profile["id"] == value["id"] else profile for profile in profiles]
            else:
                profiles.append(value)
        self.data = self._validate(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".settings-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(self.data, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def install(store, settings, ai=None, auth=None):
    """Install a specific profile independently of model or SDK availability."""
    from .bundle import build_provider

    if ai is None:
        from pythona import ai
    value = validate(settings)
    saved = next((profile for profile in store.data["profiles"] if profile["id"] == value["id"]), None)
    if saved and value["backend"] != saved["backend"]:
        raise ValueError("The backend of an existing provider cannot be changed")
    value["provider_id"] = saved["provider_id"] if saved else value["provider_id"]
    runtime = provider_settings(value, store.service)
    script = build_provider(runtime)
    provider_id = value["provider_id"]
    storage = {}
    if provider_id:
        try:
            storage.update(ai.get_custom_provider(provider_id)["local_storage"])
        except KeyError:
            provider_id = None
    if value["backend"] in CLOUD_BACKENDS:
        key = CHATGPT_STORAGE_KEY if value["backend"] == "chatgpt" else CODEX_STORAGE_KEY
        storage[key] = json.dumps(runtime, ensure_ascii=False)
        if auth is not None:
            write_credentials(value["backend"], storage, auth)
        error = authorization_error(value["backend"], read_credentials(value["backend"], storage))
        if error:
            from .codex_auth import AuthError
            raise AuthError(error)
    else:
        storage[STORAGE_KEY] = json.dumps(runtime, ensure_ascii=False)
    if provider_id:
        ai.update_custom_provider(provider_id, name=value["name"], js_code=script, local_storage=storage)
    else:
        provider_id = ai.create_custom_provider(name=value["name"], js_code=script, local_storage=storage)
    value["provider_id"] = provider_id
    try:
        store.save(value)
    except OSError as error:
        raise RuntimeError(f"Provider saved, but the local record could not be written. Keep this ID: {provider_id}\n{error}") from error
    return provider_id
