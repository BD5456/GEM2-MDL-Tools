# -*- coding: utf-8 -*-
"""
i18n — Internationalization module for GEM2 Engine Tools.
Provides _() translation function for three languages: en, zh, ru.

Language detection order:
  1. Blender translation locale (bpy.app.translations.locale)
  2. Blender UI language (bpy.context.preferences.view.language)
  3. System environment variable GEM2_LANG
  4. Fallback: en

Usage:
    from .i18n import _
    print(_("key.name"))
    msg = _("key.with_params", param1=value1, param2=value2)
"""

import os
import json

__all__ = ("_", "LANG", "available_langs")

_LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
_CACHE = {}
_LANG = None
_FALLBACK_LANG = "en"


def _detect_language():
    """Detect preferred language from Blender or environment."""
    try:
        import bpy
        # Try Blender translation locale first
        try:
            locale = bpy.app.translations.locale
            if locale:
                if isinstance(locale, bytes):
                    locale = locale.decode("utf-8", errors="replace")
                lang = str(locale).split("_")[0].lower()
                if lang in ("zh", "zh_CN", "zh_CN.UTF-8", "zh_CN.utf8"):
                    return "zh"
                if lang in ("ru", "ru_RU"):
                    return "ru"
        except Exception:
            pass
        # Try Blender UI language preference
        try:
            prefs = bpy.context.preferences.view
            lang_code = prefs.language
            if lang_code == "zh_CN" or lang_code == "zh_HANS":
                return "zh"
            if lang_code == "ru" or lang_code == "ru_RU":
                return "ru"
        except Exception:
            pass
    except ImportError:
        pass

    # Environment variable override
    env_lang = os.environ.get("GEM2_LANG", "").lower()
    if env_lang in ("zh", "zh_cn", "chinese"):
        return "zh"
    if env_lang in ("ru", "russian"):
        return "ru"

    return _FALLBACK_LANG


def _load_locale(lang):
    """Load locale JSON file, returning a flat dict."""
    if lang in _CACHE:
        return _CACHE[lang]

    path = os.path.join(_LOCALE_DIR, lang + ".json")
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    _CACHE[lang] = data
    return data


def _(key, **kwargs):
    """Get localized string by key.
    
    Supports str.format() style parameters.
    Example: _("export.success", dir="/path/to/export")
    """
    lang = LANG
    strings = _load_locale(lang)

    if key in strings:
        text = strings[key]
    else:
        # Fallback to English
        if lang != _FALLBACK_LANG:
            fallback = _load_locale(_FALLBACK_LANG)
            text = fallback.get(key, key)
        else:
            text = key

    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError):
            pass

    return text


# Detect language once at module load
LANG = _detect_language()


def available_langs():
    """Return list of available language codes."""
    langs = []
    for fname in os.listdir(_LOCALE_DIR):
        if fname.endswith(".json"):
            langs.append(fname[:-5])
    return sorted(langs)
