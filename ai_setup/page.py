"""Render the bundled page for WebKit or a desktop browser preview."""

import json
from pathlib import Path

from .l10n import STRINGS, resolve_language


WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def render_page(language="en", preview=False):
    language = resolve_language(language)
    bootstrap = json.dumps({"language": language, "strings": {**STRINGS["en"], **STRINGS[language]},
                            "preview": preview}, ensure_ascii=False).replace("<", "\\u003c")
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    return (html.replace("/* STYLES */", (WEB_ROOT / "style.css").read_text(encoding="utf-8"))
            .replace("/* BOOTSTRAP */", "window.SETUP = " + bootstrap + ";")
            .replace("/* SCRIPT */", (WEB_ROOT / "app.js").read_text(encoding="utf-8")))
