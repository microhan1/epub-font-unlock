"""Language file loader for epub-font-unlock.

All user-facing strings live in lang/<code>.json. Use ``t(key, **kwargs)``
to look one up; ``{placeholders}`` are filled from kwargs.
"""
from __future__ import annotations

import json
import locale
import os
import sys

LANGS = ("ko", "en", "zh-CN", "ja")
LANG_NAMES = {"ko": "한국어", "en": "English", "zh-CN": "简体中文", "ja": "日本語"}
DEFAULT_LANG = "en"


def resource_dir() -> str:
    """Folder holding lang/ (inside the PyInstaller bundle when frozen)."""
    return getattr(sys, "_MEIPASS", app_dir())


def app_dir() -> str:
    """Folder next to the executable (or main.py). settings.json lives here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_PATH = os.path.join(app_dir(), "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def detect_os_lang() -> str:
    """Pick one of LANGS from the OS UI language. Falls back to English."""
    code = None
    if sys.platform == "win32":
        try:
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            code = {0x12: "ko", 0x11: "ja", 0x04: "zh-CN", 0x09: "en"}.get(lcid & 0x3FF)
        except Exception:
            code = None
    if code is None:
        loc = ""
        for env in ("LC_ALL", "LC_MESSAGES", "LANG"):
            if os.environ.get(env):
                loc = os.environ[env]
                break
        if not loc:
            try:
                loc = locale.getlocale()[0] or ""
            except Exception:
                loc = ""
        loc = loc.replace("-", "_").lower()
        if loc.startswith(("ko", "korean")):
            code = "ko"
        elif loc.startswith(("ja", "japanese")):
            code = "ja"
        elif loc.startswith(("zh", "chinese")):
            code = "zh-CN"
        elif loc.startswith(("en", "english")):
            code = "en"
    return code or DEFAULT_LANG


class _I18n:
    def __init__(self) -> None:
        self.lang = DEFAULT_LANG
        self._strings: dict[str, str] = {}
        self._fallback: dict[str, str] = self._load_file(DEFAULT_LANG)

    @staticmethod
    def _load_file(lang: str) -> dict[str, str]:
        path = os.path.join(resource_dir(), "lang", f"{lang}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def set_lang(self, lang: str, persist: bool = True) -> None:
        if lang not in LANGS:
            lang = DEFAULT_LANG
        self.lang = lang
        self._strings = self._load_file(lang)
        if persist:
            settings = load_settings()
            settings["lang"] = lang
            save_settings(settings)

    def t(self, key: str, **kwargs) -> str:
        text = self._strings.get(key) or self._fallback.get(key) or key
        if not kwargs:
            return text
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text


_inst = _I18n()


def init(lang: str | None = None) -> str:
    """Load the language: explicit > settings.json > OS language > English."""
    if lang is None:
        lang = load_settings().get("lang")
    if lang not in LANGS:
        lang = detect_os_lang()
    _inst.set_lang(lang, persist=False)
    return lang


def set_lang(lang: str, persist: bool = True) -> None:
    _inst.set_lang(lang, persist=persist)


def current_lang() -> str:
    return _inst.lang


def t(key: str, **kwargs) -> str:
    return _inst.t(key, **kwargs)
