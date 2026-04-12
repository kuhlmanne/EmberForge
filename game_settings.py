"""
game_settings.py - Single source of truth for all Ember Forge settings.

Consolidates: .env keys, LLM config, TTS config, save dir, game prefs.
One file: .game_settings.json. Auto-imports from .env on first run.

On startup: if .game_settings.json doesn't exist, reads .env and creates
it with all discovered keys. After that, the settings file is canonical.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

WINGMAN_DIR = Path(__file__).parent
SETTINGS_FILE = WINGMAN_DIR / ".game_settings.json"

DEFAULT_SETTINGS = {
    # API Keys
    "anthropic_api_key": "",
    "elevenlabs_api_key": "",
    "openai_api_key": "",

    # LLM
    "llm_provider": "anthropic",
    "llm_model_keeper": "claude-sonnet-4-6",
    "llm_model_forge": "claude-haiku-4-5-20251001",
    "llm_base_url": "",

    # TTS
    "tts_provider": "browser",  # off | browser | edge_tts | elevenlabs
    "keeper_voice_id": "",

    # Game
    "dice_feedback": "subtle",  # off | subtle | visible
    "ambience": "subtle",       # off | subtle | full
    "companion_hints": True,

    # Save
    "save_directory": "",       # empty = local .saves/
    "active_slot": 0,

    # Player
    "player_name": "",
    "birthday": "",
    "steam_integration": False,
}


def _import_from_env() -> dict:
    """Read .env file and extract known keys."""
    imported = {}
    env_path = WINGMAN_DIR / ".env"
    if not env_path.exists():
        return imported
    try:
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "ANTHROPIC_API_KEY" and v:
                imported["anthropic_api_key"] = v
                os.environ["ANTHROPIC_API_KEY"] = v
            elif k == "ELEVENLABS_API_KEY" and v:
                imported["elevenlabs_api_key"] = v
                os.environ["ELEVENLABS_API_KEY"] = v
            elif k == "OPENAI_API_KEY" and v:
                imported["openai_api_key"] = v
            elif k == "KEEPER_VOICE_ID" and v:
                imported["keeper_voice_id"] = v
    except Exception:
        pass
    return imported


def load() -> dict:
    """Load settings. On first run, imports from .env."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge with defaults (new keys added in updates)
            settings = dict(DEFAULT_SETTINGS)
            settings.update(saved)
            _apply_to_env(settings)
            return settings
        except Exception:
            pass

    # First run: import from .env
    settings = dict(DEFAULT_SETTINGS)
    imported = _import_from_env()
    settings.update(imported)
    save(settings)
    return settings


def save(settings: dict) -> None:
    """Persist settings to disk."""
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    tmp.replace(SETTINGS_FILE)
    _apply_to_env(settings)


def get(key: str, default=None):
    """Get a single setting."""
    return load().get(key, default)


def set_value(key: str, value) -> dict:
    """Set a single setting and persist."""
    settings = load()
    settings[key] = value
    save(settings)
    return settings


def update(updates: dict) -> dict:
    """Merge multiple settings and persist."""
    settings = load()
    settings.update(updates)
    save(settings)
    return settings


def _apply_to_env(settings: dict) -> None:
    """Push API keys into os.environ so existing code picks them up."""
    if settings.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = settings["anthropic_api_key"]
    if settings.get("elevenlabs_api_key"):
        os.environ["ELEVENLABS_API_KEY"] = settings["elevenlabs_api_key"]
    if settings.get("openai_api_key"):
        os.environ["OPENAI_API_KEY"] = settings["openai_api_key"]


def get_safe() -> dict:
    """Return settings with API keys masked for UI display."""
    settings = dict(load())
    for key in ["anthropic_api_key", "elevenlabs_api_key", "openai_api_key"]:
        val = settings.get(key, "")
        if val and len(val) > 10:
            settings[key + "_display"] = val[:6] + "..." + val[-4:]
            settings[key + "_set"] = True
        else:
            settings[key + "_display"] = "(not set)"
            settings[key + "_set"] = bool(val)
        del settings[key]  # never send raw keys to frontend
    return settings
