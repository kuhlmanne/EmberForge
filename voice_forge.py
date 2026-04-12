"""
voice_forge.py - Generate unique voices for forged personas via ElevenLabs.

When the Ember Forge creates a new companion, this module generates a
unique voice for them using ElevenLabs Voice Design API. The voice is
saved to the user's ElevenLabs library and the voice_id is stored with
the persona for all future TTS calls.

Falls back gracefully if no ElevenLabs key is configured — personas
use browser TTS or Edge TTS instead.

Requires: ELEVENLABS_API_KEY in .env
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional


def _get_el_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _voice_description_from_persona(persona: dict) -> dict:
    """Derive ElevenLabs Voice Design parameters from a persona's traits."""
    essence = (persona.get("essence") or "").lower()
    archetype = (persona.get("archetype") or "wanderer").lower()
    tags = persona.get("affinity_tags") or []

    # Gender heuristic from the name/essence (imperfect but functional)
    female_hints = ["she", "her", "woman", "girl", "mother", "sister", "queen", "witch", "priestess"]
    male_hints = ["he", "him", "man", "boy", "father", "brother", "king", "warrior"]
    name = (persona.get("display_name") or "").lower()

    is_female = any(h in essence for h in female_hints) or any(h in name for h in ["a", "e", "i"])
    is_male = any(h in essence for h in male_hints)
    gender = "female" if (is_female and not is_male) else "male" if is_male else "female"

    # Age from archetype
    old_archetypes = {"scholar", "sage", "elder", "keeper", "guardian", "mystic"}
    young_archetypes = {"rebel", "wanderer", "scout", "rogue", "spark"}
    if archetype in old_archetypes:
        age = "old"
    elif archetype in young_archetypes:
        age = "young"
    else:
        age = "middle_aged"

    # Accent from origin/tags
    accent = "american"  # default
    origin = (persona.get("origin") or "").lower()
    if any(w in origin for w in ["british", "england", "london", "oxford"]):
        accent = "british"
    elif any(w in origin for w in ["australia", "sydney"]):
        accent = "australian"
    elif any(w in origin for w in ["india", "mumbai", "delhi"]):
        accent = "indian"

    # Build a text description for the Voice Design prompt
    tag_words = ", ".join(tags[:3]) if tags else "balanced"
    desc = (
        f"A {gender} voice, {age}, {accent} accent. "
        f"Personality: {tag_words}. "
        f"Archetype: {archetype}. "
        f"{persona.get('essence', '')[:200]}"
    )

    return {
        "gender": gender,
        "age": age,
        "accent": accent,
        "accent_strength": 1.0,
        "description": desc,
        # Text must be >= 100 chars for ElevenLabs Voice Design
        "text": _build_sample_text(persona),
    }


def _build_sample_text(persona: dict) -> str:
    """Build a sample text >= 100 chars for Voice Design from the persona's
    quotes and essence. ElevenLabs requires at least 100 characters."""
    parts = []
    for q in (persona.get("signature_quotes") or [])[:3]:
        parts.append(str(q))
    parts.append(persona.get("essence") or "I am here now. That is what matters.")
    text = " ".join(parts)
    # Pad if still too short
    while len(text) < 110:
        text += " I have been waiting. Not long. Not impatiently."
    return text[:500]


def generate_voice(persona: dict) -> Optional[str]:
    """Generate a unique voice for a persona via ElevenLabs Voice Design.
    Returns the voice_id if successful, None if ElevenLabs is unavailable.

    The voice is created using the text-to-voice preview endpoint, then
    saved to the user's voice library with the persona's name.
    """
    api_key = _get_el_key()
    if not api_key:
        return None

    params = _voice_description_from_persona(persona)
    display_name = persona.get("display_name", "Forged Voice")

    try:
        # Step 1: Generate voice preview
        preview_body = json.dumps({
            "voice_description": params["description"],
            "text": params["text"],
        }).encode()

        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/text-to-voice/create-previews",
            data=preview_body,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())

        previews = result.get("previews") or []
        if not previews:
            print(f"[voice_forge] no previews returned for {display_name}")
            return None

        # Use the first preview
        preview = previews[0]
        generated_voice_id = preview.get("generated_voice_id")

        if not generated_voice_id:
            print(f"[voice_forge] no generated_voice_id in preview")
            return None

        # Step 2: Save the voice to the library
        save_body = json.dumps({
            "voice_name": f"Amplify - {display_name}",
            "voice_description": f"Generated companion voice for {display_name}",
            "generated_voice_id": generated_voice_id,
        }).encode()

        save_req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/text-to-voice/create-voice-from-preview",
            data=save_body,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            },
        )

        with urllib.request.urlopen(save_req, timeout=15) as r:
            save_result = json.loads(r.read())

        voice_id = save_result.get("voice_id")
        if voice_id:
            print(f"[voice_forge] created voice for {display_name}: {voice_id}")
            return voice_id
        else:
            print(f"[voice_forge] save returned no voice_id")
            return None

    except Exception as e:
        print(f"[voice_forge] failed for {display_name}: {e}")
        return None


def speak_with_voice(text: str, voice_id: str) -> Optional[bytes]:
    """Generate speech audio using a specific ElevenLabs voice_id.
    Returns raw MP3 bytes, or None on failure."""
    api_key = _get_el_key()
    if not api_key or not voice_id:
        return None

    try:
        body = json.dumps({
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.3,
            },
        }).encode()

        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            data=body,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    except Exception as e:
        print(f"[voice_forge] TTS failed: {e}")
        return None
