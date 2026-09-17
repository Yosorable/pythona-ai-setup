"""Persist this project's known provider ID without enumerating other connections."""

import copy
import json
import os
from pathlib import Path
import secrets
import tempfile


STORAGE_KEY = "apple_fm_settings"
STATE_PATH = Path(__file__).resolve().parents[1] / "settings.local.json"


def defaults():
    return {
        "name": "Apple On-Device Model",
        "provider_id": None,
        "groups": {"files": True, "browser": False, "python": False},
        "port": 8768,
        "idle_seconds": 300,
        "request_seconds": 120,
        "maximum_response_tokens": 800,
        "service_token": secrets.token_urlsafe(24),
    }


def validate(settings):
    value = copy.deepcopy(settings)
    value["name"] = str(value["name"]).strip()
    if not value["name"]:
        raise ValueError("Connection name must not be empty")
    if not isinstance(value["groups"], dict):
        raise ValueError("Invalid tool group settings")
    for group in ("files", "browser", "python"):
        if type(value["groups"].get(group)) is not bool:
            raise ValueError(f"Invalid tool group setting: {group}")
    for key, minimum, maximum in (
        ("port", 1024, 65535), ("idle_seconds", 5, 86400),
        ("request_seconds", 1, 3600), ("maximum_response_tokens", 1, 8192),
    ):
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError(f"Setting out of range: {key}")
    if not isinstance(value["service_token"], str) or len(value["service_token"]) < 16:
        raise ValueError("Invalid local service credentials")
    if value["provider_id"] is not None and not isinstance(value["provider_id"], str):
        raise ValueError("Invalid saved provider ID")
    return value


def provider_settings(settings):
    return {key: value for key, value in validate(settings).items()
            if key not in ("name", "provider_id")}


class SettingsStore:
    def __init__(self, path=STATE_PATH):
        self.path = Path(path)
        self.value = defaults()
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError) as error:
            raise RuntimeError(f"Could not read settings; the original file was preserved: {self.path}\n{error}") from error
        if not isinstance(saved, dict):
            raise ValueError(f"Settings must be a JSON object: {self.path}")
        self.value.update(saved)
        self.value = validate(self.value)

    def save(self, value=None):
        # Retain the ID in memory so a retry after a disk failure does not create duplicates.
        self.value = validate(self.value if value is None else value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".settings-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(self.value, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def install(store, settings, ai=None):
    """Install regardless of probe results; import pythona.ai only when needed."""
    from .bundle import build_provider

    if ai is None:
        from pythona import ai
    value = validate(settings)
    # An outdated UI snapshot must not overwrite the installation's current ID.
    value["provider_id"] = store.value["provider_id"]
    store.save(value)
    script = build_provider(provider_settings(value))
    provider_id = value["provider_id"]
    storage = {}
    if provider_id:
        try:
            storage.update(ai.get_custom_provider(provider_id)["local_storage"])
        except KeyError:
            provider_id = None
    storage[STORAGE_KEY] = json.dumps(provider_settings(value), ensure_ascii=False)
    if provider_id:
        ai.update_custom_provider(provider_id, name=value["name"], js_code=script,
                                  local_storage=storage)
    else:
        provider_id = ai.create_custom_provider(name=value["name"], js_code=script,
                                               local_storage=storage)
    store.value["provider_id"] = provider_id
    try:
        store.save()
    except OSError as error:
        raise RuntimeError(f"Provider saved, but the local record could not be written. Keep this ID: {provider_id}\n{error}") from error
    return provider_id
