# -*- coding: utf-8 -*-
"""
i18n — Internationalization module for GEM2 Engine Tools.
Provides _() translation function for four languages: en, zh, ru, uk.

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
                lang = str(locale).replace('-', '_').split('_')[0].lower()
                if lang == "zh":
                    return "zh"
                if lang == "ru":
                    return "ru"
                if lang == "uk":
                    return "uk"
                if lang == "en":
                    return "en"
        except Exception:
            pass
        # Try Blender UI language preference
        try:
            prefs = bpy.context.preferences.view
            lang_code = prefs.language
            normalized = str(lang_code).replace('-', '_').lower()
            if normalized in ("zh_cn", "zh_hans"):
                return "zh"
            if normalized in ("ru", "ru_ru"):
                return "ru"
            if normalized in ("uk", "uk_ua"):
                return "uk"
            if normalized in ("en", "en_gb", "en_us"):
                return "en"
        except Exception:
            pass
    except ImportError:
        pass

    # Environment variable override
    env_lang = os.environ.get("GEM2_LANG", "").lower()
    if env_lang in ("zh", "zh_cn", "chinese"):
        return "zh"
    if env_lang in ("ru", "ru_ru", "russian"):
        return "ru"
    if env_lang in ("uk", "uk_ua", "ua", "ukrainian"):
        return "uk"
    if env_lang in ("en", "en_us", "en_gb", "english"):
        return "en"

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
    global LANG
    detected = _detect_language()
    if detected != LANG:
        LANG = detected
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
