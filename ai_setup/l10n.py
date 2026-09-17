"""Standalone UI catalog using the app's language, with English as the fallback."""

import json
from pathlib import Path


STRINGS = json.loads(Path(__file__).with_name("strings.json").read_text(encoding="utf-8"))


def resolve_language(language):
    normalized = str(language).replace("_", "-").lower()
    if normalized.startswith("zh"):
        parts = normalized.split("-")
        if "hans" in parts:
            return "zh-Hans"
        return "zh-Hant" if "hant" in parts or any(region in parts for region in ("tw", "hk", "mo")) else "zh-Hans"
    base = normalized.split("-")[0]
    return base if base in STRINGS else "en"


def app_language():
    # The main bundle respects per-app language settings; no UserDefaults reads are needed.
    from rubicon.objc import ObjCClass
    bundle = ObjCClass("NSBundle").mainBundle
    bundle = bundle() if callable(bundle) else bundle
    languages = bundle.preferredLocalizations
    languages = languages() if callable(languages) else languages
    return resolve_language(languages[0]) if len(languages) else "en"


class Localizer:
    def __init__(self, language):
        self.language = resolve_language(language)

    def __call__(self, key, **values):
        text = STRINGS[self.language].get(key, STRINGS["en"][key])
        return text.format(**values)

    def reason(self, reason):
        key = "reason_" + str(reason)
        return self(key) if key in STRINGS["en"] else str(reason)

    def error(self, error):
        # Keep SDK/network diagnostics intact; translate known user-facing errors.
        text = str(error)
        if text == "Connection name must not be empty":
            return self("empty_name")
        if text == "Enter a Hugging Face model ID":
            return self("invalid_model_id")
        if text == "Remove this provider in Pythona's AI Assistant settings first":
            return self("remove_help")
        for reason in ("APPLE_INTELLIGENCE_NOT_ENABLED", "DEVICE_NOT_ELIGIBLE", "MODEL_NOT_READY", "UNKNOWN"):
            if text.endswith("Apple on-device model unavailable: " + reason):
                return self("unavailable", reason=self.reason(reason))
        return text
