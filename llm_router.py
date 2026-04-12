"""
llm_router.py - Unified LLM interface for Amplify.

Supports:
  - anthropic:     Claude models via Anthropic API (default)
  - openai_compat: Any OpenAI-compatible endpoint (OpenAI, Ollama, Together, Groq)

Players configure their provider in Settings. The router translates
between formats so session_forge and memento_forge don't need to know
which provider is active.

No new dependencies — uses urllib for all HTTP calls, just like the
existing session_forge code.

Config is read from .llm_config.json in the active save slot (or
wingman root). Falls back to ANTHROPIC_API_KEY from os.environ.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Optional

WINGMAN_DIR = Path(__file__).parent
CONFIG_FILE = WINGMAN_DIR / ".llm_config.json"

# Defaults
DEFAULT_CONFIG = {
    "provider":      "anthropic",
    "model_keeper":  "claude-sonnet-4-6",
    "model_forge":   "claude-haiku-4-5-20251001",
    "api_key":       "",           # empty = use ANTHROPIC_API_KEY from env
    "base_url":      "",           # empty = use provider default
}

# Provider default base URLs
PROVIDER_URLS = {
    "anthropic":     "https://api.anthropic.com",
    "openai_compat": "https://api.openai.com",
}


def _load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    # Try slot-specific config first, then root
    for path in [CONFIG_FILE]:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                cfg.update({k: v for k, v in saved.items() if v})
            except Exception:
                pass
            break
    # Try game_settings.json
    try:
        import game_settings
        gs = game_settings.load()
        if gs.get("anthropic_api_key") and not cfg.get("api_key"):
            cfg["api_key"] = gs["anthropic_api_key"]
        if gs.get("llm_provider"):
            cfg["provider"] = gs["llm_provider"]
        if gs.get("llm_model_keeper"):
            cfg["model_keeper"] = gs["llm_model_keeper"]
        if gs.get("llm_model_forge"):
            cfg["model_forge"] = gs["llm_model_forge"]
    except Exception:
        pass
    # Fallback: API key from environment
    if not cfg.get("api_key"):
        cfg["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg


def save_config(updates: dict) -> dict:
    """Merge updates into the config and persist. Returns the full config."""
    cfg = _load_config()
    cfg.update({k: v for k, v in updates.items() if k in DEFAULT_CONFIG})
    # Also set the API key in env so existing code picks it up
    if cfg.get("api_key"):
        os.environ["ANTHROPIC_API_KEY"] = cfg["api_key"]
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg


def get_config() -> dict:
    """Return current config (sanitized — key is masked for UI display)."""
    cfg = _load_config()
    safe = dict(cfg)
    key = safe.get("api_key", "")
    if key and len(key) > 8:
        safe["api_key_display"] = key[:7] + "..." + key[-4:]
    else:
        safe["api_key_display"] = "(not set)"
    safe.pop("api_key", None)  # never send raw key to frontend
    return safe


def call(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 300,
    role: str = "keeper",   # "keeper" or "forge" — picks the model
) -> str:
    """Unified LLM call. Handles both Anthropic and OpenAI-compatible formats.

    messages: list of {"role": "user"|"assistant", "content": "..."}
    system:   system prompt (handled differently per provider)
    role:     "keeper" uses model_keeper, "forge" uses model_forge
    max_tokens: max response tokens

    Returns the text response.
    Raises RuntimeError on failure.
    """
    cfg = _load_config()
    provider = cfg.get("provider", "anthropic")
    model = cfg.get("model_keeper") if role == "keeper" else cfg.get("model_forge")
    api_key = cfg.get("api_key", "")
    base_url = cfg.get("base_url", "").rstrip("/") or PROVIDER_URLS.get(provider, "")

    if not api_key and provider != "openai_compat":
        raise RuntimeError("No API key configured. Set one in Settings.")

    if provider == "anthropic":
        return _call_anthropic(base_url, api_key, model, system, messages, max_tokens)
    else:
        return _call_openai_compat(base_url, api_key, model, system, messages, max_tokens)


def _call_anthropic(base_url: str, api_key: str, model: str,
                     system: str, messages: list, max_tokens: int) -> str:
    """Call Anthropic's messages API."""
    url = f"{base_url}/v1/messages"
    body = {
        "model": model or "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        body["system"] = system
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read())
    text = "".join(
        block.get("text", "")
        for block in resp.get("content", [])
        if block.get("type") == "text"
    )
    return text.strip()


def _call_openai_compat(base_url: str, api_key: str, model: str,
                         system: str, messages: list, max_tokens: int) -> str:
    """Call an OpenAI-compatible chat completions endpoint.
    Works with: OpenAI, Ollama, Together, Groq, LM Studio, etc."""
    url = f"{base_url}/v1/chat/completions"
    # Convert to OpenAI format: system message goes first in messages array
    oai_messages = []
    if system:
        oai_messages.append({"role": "system", "content": system})
    oai_messages.extend(messages)
    body = {
        "model": model or "gpt-4o",
        "messages": oai_messages,
        "max_tokens": max_tokens,
    }
    data = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read())
    choices = resp.get("choices", [])
    if not choices:
        raise RuntimeError(f"No response from {model}")
    return (choices[0].get("message", {}).get("content", "") or "").strip()


# ── Preset configs for common providers ──────────────────────────────
PRESETS = {
    "anthropic": {
        "provider": "anthropic",
        "model_keeper": "claude-sonnet-4-6",
        "model_forge": "claude-haiku-4-5-20251001",
        "base_url": "",
    },
    "openai": {
        "provider": "openai_compat",
        "model_keeper": "gpt-4o",
        "model_forge": "gpt-4o-mini",
        "base_url": "https://api.openai.com",
    },
    "ollama": {
        "provider": "openai_compat",
        "model_keeper": "llama3.1",
        "model_forge": "llama3.1",
        "base_url": "http://localhost:11434",
        "api_key": "ollama",
    },
    "together": {
        "provider": "openai_compat",
        "model_keeper": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "model_forge": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "base_url": "https://api.together.xyz",
    },
}
