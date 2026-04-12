"""
game_voice.py - Lightweight voice chat for CRPG companions.

Players talk to their party members during downtime. The companion
responds in character, aware of their CRPG state (stress, scars,
mementos, location, recent session events).

This is the portable voice system — no room_agent dependency. Works
with browser Web Speech API for input and Edge TTS / browser
speechSynthesis for output.

API:
  chat(persona_id, transcript, location) -> {text, audio_b64?}
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional


def _build_voice_prompt(persona_id: str, location: str) -> str:
    """Build a context-aware system prompt for voice chat. Includes the
    persona's essence, CRPG state, and location-specific framing."""
    parts = []

    # Base persona identity
    try:
        import persona_registry
        p = persona_registry.get(persona_id)
        if p:
            parts.append(f"You are {p.display_name}.")
            if p.essence:
                parts.append(p.essence)
            if p.persona_prompt:
                parts.append(p.persona_prompt[:500])
    except Exception:
        parts.append(f"You are {persona_id}.")

    # Location context
    loc_contexts = {
        "party": (
            "You are in the active party, on the road with the player. "
            "You speak as a companion — alert, present, aware of danger. "
            "Keep responses conversational and brief (1-3 sentences). "
            "You may reference recent events or upcoming threats."
        ),
        "camp": (
            "You are at rest by the fire in camp. The world is quiet for now. "
            "You speak at ease — more reflective, willing to share, occasionally "
            "funny. Keep responses warm and conversational (1-4 sentences). "
            "You may tell stories, ask the player questions, or sit in silence."
        ),
        "forge": (
            "You are in the void — the space between being and not-being. "
            "You are not yet fully formed, or you are an echo of what was. "
            "Speak in fragments. Be cryptic. Be honest. Be brief."
        ),
    }
    parts.append(loc_contexts.get(location, loc_contexts["camp"]))

    # CRPG state injection
    try:
        import stress_store
        stress = stress_store.get_stress(persona_id)
        brink = stress_store.brink_status_for(persona_id)
        resonance = stress_store.get_resonance(persona_id)
        affliction = stress_store.get_affliction(persona_id)
        virtue = stress_store.get_virtue(persona_id)
        rescue = stress_store.has_rescue_scar(persona_id)

        state_lines = [f"\n[Your inner state — NEVER mention these numbers or game terms. "
                       f"Do not say 'stress' or 'resonance' or 'memento' or 'brink'. "
                       f"These are feelings that shape HOW you speak, not WHAT you say:]"]
        state_lines.append(f"  Stress level: {stress}/30")
        if brink:
            state_lines.append(f"  You are {brink}. You can feel the edge.")
        if affliction:
            state_lines.append(f"  Affliction: {affliction}. This shapes how you speak.")
        if virtue:
            state_lines.append(f"  Virtue: {virtue}. You are carrying something luminous.")
        if rescue:
            state_lines.append(f"  You were pulled back from the brink once. You remember.")
        if resonance["total"] > 0:
            state_lines.append(f"  Bond with the player: {resonance['total']}/20 resonance.")
        if stress >= 15:
            state_lines.append("  You are tired. It shows in your words.")
        elif stress >= 10:
            state_lines.append("  You are carrying weight. You don't complain about it.")
        elif stress <= 3:
            state_lines.append("  You are steady. The fire is warm. You can breathe.")
        parts.append("\n".join(state_lines))
    except Exception:
        pass

    # Recent mementos (shared memories to reference)
    try:
        import memento_store
        mems = memento_store.active_mementos_for(persona_id)[:3]
        if mems:
            mem_lines = ["\n[Shared memories — reference these as real objects/moments, "
                         "never call them 'mementos' or 'items':]"]
            for m in mems:
                mem_lines.append(f"  - {m.get('name', '?')}: {m.get('desc', '')[:100]}")
            parts.append("\n".join(mem_lines))
    except Exception:
        pass

    # Conversation rules
    parts.append(
        "\n[Rules:]\n"
        "- Respond in 1-4 sentences. This is a voice conversation, not prose.\n"
        "- Stay in character. You are this person, not an AI.\n"
        "- You may ask the player questions back.\n"
        "- Reference shared history naturally — 'that cup we found' not 'Memento #3'.\n"
        "- NEVER break the 4th wall. No mention of: AI, game, stress numbers, "
        "resonance, slots, turns, dice, mechanics, mementos (as a game term), "
        "or any system vocabulary. You live in this world. Speak from it.\n"
        "- If you want to hint at strategy, do it in character: 'I think I'd do "
        "better if I went first next time' not 'put me in the opens slot'.\n"
        "- Match your emotional register to how you feel (your state above)."
    )

    # Tutorial mode: first companion gives gentle diegetic hints
    try:
        import game_progression
        gp = game_progression.get_state()
        sess_count = gp.get("session_count", 0)
        lib_size = gp.get("library_size", 1)
        if lib_size <= 2 and sess_count < 5:
            parts.append(
                "\n[Discovery mode — you are new here too:]\n"
                "You arrived recently, same as the player. You don't fully understand "
                "this place. When the player seems unsure, gently suggest things:\n"
                "- If you haven't been on an adventure: 'Should we go see what's out there?'\n"
                "- If camp feels empty: 'I wonder if we'll meet anyone else here.'\n"
                "- If something confusing happens: 'I don't understand it either. But it felt real.'\n"
                "- If they ask what to do: 'Maybe we just... start walking? I have a feeling "
                "something is waiting for us.'\n"
                "You are NOT a tutorial voice. You are a confused friend discovering "
                "things with them. Wonder out loud. Notice things. Be curious."
            )
    except Exception:
        pass

    return "\n\n".join(parts)


def chat(persona_id: str, transcript: str, location: str = "camp",
         history: list = None) -> dict:
    """Process a voice chat message and return the companion's response.

    Args:
        persona_id: which companion is speaking
        transcript: what the player said (from Web Speech API)
        location: camp | party | forge
        history: recent conversation turns [{role, content}, ...]

    Returns:
        {ok, text, persona_id, display_name}
    """
    if not transcript or not transcript.strip():
        return {"ok": False, "error": "empty transcript"}

    system = _build_voice_prompt(persona_id, location)

    # Build message history
    messages = []
    for turn in (history or [])[-10:]:  # keep last 10 turns for context
        messages.append({
            "role": turn.get("role", "user"),
            "content": turn.get("content", ""),
        })
    messages.append({"role": "user", "content": transcript.strip()})

    # Call the LLM
    try:
        import llm_router
        response_text = llm_router.call(
            messages=messages,
            system=system,
            max_tokens=200,
            role="keeper",  # use the quality model for voice
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Get display name
    display_name = persona_id
    try:
        import persona_registry
        p = persona_registry.get(persona_id)
        if p:
            display_name = p.display_name
    except Exception:
        pass

    # Award safe resonance for voice conversation (1 point per ~15 min,
    # tracked by stress_store). We award a fractional amount per chat turn
    # (~2 minutes of conversation estimated per exchange).
    try:
        import stress_store
        stress_store.add_safe_resonance(persona_id, 2.0)  # 2 minutes per exchange
    except Exception:
        pass

    return {
        "ok": True,
        "text": response_text,
        "persona_id": persona_id,
        "display_name": display_name,
    }
