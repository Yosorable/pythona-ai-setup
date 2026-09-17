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
STATE_PATH = Path(__file__).resolve().parents[1] / "settings.local.json"
DEFAULT_MLX_MODEL = "mlx-community/Qwen3-1.7B-4bit"
BACKENDS = ("apple_fm", "mlx_lm")


def service_defaults():
    return {"port": 8768, "idle_seconds": 300, "request_seconds": 120, "load_seconds": 900,
            "service_token": secrets.token_urlsafe(24)}


def defaults(backend="apple_fm"):
    if backend not in BACKENDS:
        raise ValueError("Unknown model backend")
    return {"id": uuid.uuid4().hex, "backend": backend,
            "name": "Apple On-Device Model" if backend == "apple_fm" else "Qwen3-1.7B (MLX)",
            "model_id": DEFAULT_MLX_MODEL if backend == "mlx_lm" else "",
            "provider_id": None,
            "groups": {"files": True, "browser": False, "python": False},
            "maximum_response_tokens": 800}


def validate(settings):
    value = copy.deepcopy(settings)
    if not isinstance(value, dict) or value.get("backend") not in BACKENDS:
        raise ValueError("Unknown model backend")
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise ValueError("Invalid profile ID")
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise ValueError("Connection name must not be empty")
    value["name"] = value["name"].strip()
    if not isinstance(value.get("groups"), dict):
        raise ValueError("Invalid tool group settings")
    for group in ("files", "browser", "python"):
        if type(value["groups"].get(group)) is not bool:
            raise ValueError(f"Invalid tool group setting: {group}")
    if not isinstance(value.get("model_id"), str):
        raise ValueError("Enter a Hugging Face model ID")
    value["model_id"] = value["model_id"].strip()
    if value["backend"] == "mlx_lm" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value["model_id"]):
        raise ValueError("Enter a Hugging Face model ID")
    if value["backend"] == "apple_fm" and value["model_id"]:
        raise ValueError("Apple Foundation Models does not accept a model ID")
    if type(value.get("maximum_response_tokens")) is not int or not 1 <= value["maximum_response_tokens"] <= 8192:
        raise ValueError("Invalid maximum_response_tokens")
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
    return {**validate_service(service), **{key: value for key, value in validate(settings).items()
            if key not in ("id", "name", "provider_id")}}


class SettingsStore:
    def __init__(self, path=STATE_PATH):
        self.path = Path(path)
        profile = defaults()
        self.data = {"selected_id": profile["id"], "service": service_defaults(), "profiles": [profile]}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError) as error:
            raise RuntimeError(f"Could not read settings; the original file was preserved: {self.path}\n{error}") from error
        self.data = self._validate(saved)

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict) or set(data) != {"selected_id", "service", "profiles"}:
            raise ValueError("Invalid settings file structure")
        if not isinstance(data["profiles"], list):
            raise ValueError("Profiles must be an array")
        profiles = [validate(value) for value in data["profiles"]]
        ids = [value["id"] for value in profiles]
        installed = [value["provider_id"] for value in profiles if value["provider_id"]]
        if len(ids) != len(set(ids)) or len(installed) != len(set(installed)):
            raise ValueError("Duplicate profile or provider IDs")
        if (profiles and data["selected_id"] not in ids) or (not profiles and data["selected_id"] is not None):
            raise ValueError("Invalid selected profile")
        return {"selected_id": data["selected_id"], "service": validate_service(data["service"]), "profiles": profiles}

    @property
    def value(self):
        return self.get(self.data["selected_id"]) if self.data["selected_id"] is not None else None

    @property
    def service(self):
        return self.data["service"]

    def get(self, profile_id):
        for profile in self.data["profiles"]:
            if profile["id"] == profile_id:
                return profile
        raise KeyError("The configuration no longer exists")

    def add(self, backend):
        profile = defaults(backend)
        self.data["profiles"].append(profile)
        self.data["selected_id"] = profile["id"]
        self.save()
        return profile

    def select(self, profile_id):
        self.get(profile_id)
        self.data["selected_id"] = profile_id
        self.save()

    def remove(self, profile_id):
        if self.get(profile_id)["provider_id"]:
            raise ValueError("Remove this provider in Pythona's AI Assistant settings first")
        self.data["profiles"] = [value for value in self.data["profiles"] if value["id"] != profile_id]
        if self.data["selected_id"] == profile_id:
            self.data["selected_id"] = self.data["profiles"][0]["id"] if self.data["profiles"] else None
        self.save()

    def save(self, value=None):
        # Retain installed IDs in memory if writing the local record fails.
        if value is not None:
            value = validate(value)
            self.get(value["id"])
            self.data["profiles"] = [value if profile["id"] == value["id"] else profile for profile in self.data["profiles"]]
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


def install(store, settings, ai=None):
    """Install a specific profile independently of model or SDK availability."""
    from .bundle import build_provider

    if ai is None:
        from pythona import ai
    value = validate(settings)
    value["provider_id"] = store.get(value["id"])["provider_id"]
    store.save(value)
    runtime = provider_settings(value, store.service)
    script = build_provider(runtime)
    provider_id = value["provider_id"]
    storage = {}
    if provider_id:
        try:
            storage.update(ai.get_custom_provider(provider_id)["local_storage"])
        except KeyError:
            provider_id = None
    storage[STORAGE_KEY] = json.dumps(runtime, ensure_ascii=False)
    if provider_id:
        ai.update_custom_provider(provider_id, name=value["name"], js_code=script, local_storage=storage)
    else:
        provider_id = ai.create_custom_provider(name=value["name"], js_code=script, local_storage=storage)
    store.get(value["id"])["provider_id"] = provider_id
    try:
        store.save()
    except OSError as error:
        raise RuntimeError(f"Provider saved, but the local record could not be written. Keep this ID: {provider_id}\n{error}") from error
    return provider_id
