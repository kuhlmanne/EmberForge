"""
game_progression.py - Library cap, arrival pacing, unlock gates, and
diegetic narration triggers.

This is the meta-progression system that controls WHEN mechanics enter
the game and HOW the player discovers them. Every unlock is narrated by
the Keeper as a diegetic discovery — the party FINDS camp skills, they
STUMBLE into introspect, they NOTICE something about the world getting
harder. No system pop-ups. No tutorials. The Keeper writes it.

Pacing shape (from design):
  Library 1 (session 1-2):   voice, stress, turns
  Library 2 (session 2-3):   first memento, first sidequest
  Library 3 (session 3-4):   affinity composition, cross-talk
  Library 4 (session 4-5):   full party, camp rest, release/sell
  Library 5 (~session 15-25): equip slots, camp skills, choice-of-3
  Library 6 (~session 30-50): introspect, forge pool
  Library 7 (~session 50+):   full roster, everything live

Arrival trigger:
  1->4 (early):  milestone-gated (session count or first memento/stress event)
  5->7 (late):   struggle-gated (avg library stress >= threshold AND
                  at least one persona >= threshold)

Each gate has:
  - A trigger condition (checked at session-end and camp-tick)
  - A Keeper unlock narration system prompt (the discovery beat)
  - A list of features that become available when the gate opens
  - A "fired" flag so it only fires once per run

Schema of .game_progression.json:
{
  "library_size": 5,
  "session_count": 0,
  "gates_fired": ["gate_memento", "gate_sidequest", ...],
  "pending_unlock_narrations": ["gate_camp_skills", ...],
  "arrival_history": [
    {"persona_id": "shadowheart", "session": 1, "ts": 1713...},
    ...
  ]
}
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

WINGMAN_DIR = Path(__file__).parent
STORE_FILE  = WINGMAN_DIR / ".game_progression.json"

_lock = threading.RLock()

# Maximum library size. The player starts with 1 and gains personas
# up to this cap over the course of the run.
LIBRARY_CAP = 7

# ── Gate definitions ─────────────────────────────────────────────────────
# Each gate has:
#   id:             unique string
#   library_min:    minimum library size to unlock (0 = always available)
#   features:       list of feature keys that become available
#   narration_hint: a short description given to the Keeper so she can
#                   write the discovery beat in her own voice. She's told
#                   WHAT unlocked but not HOW to narrate it.

GATES = [
    {
        "id": "gate_stress",
        "library_min": 1,
        "session_min": 0,
        "features": ["stress_visible"],
        "narration_hint": (
            "The player can now see their persona's stress bar. The mechanic "
            "of stress accumulation has entered the game. Write a one-line "
            "observation — the Keeper notices the first weight landing."
        ),
    },
    {
        "id": "gate_memento",
        "library_min": 2,
        "session_min": 0,
        "features": ["mementos_visible", "sidequests_visible"],
        "narration_hint": (
            "The first memento has been forged between two party members. "
            "The player can now see mementos in inventory. Each memento "
            "carries a sidequest — a condition it wants to be spent in. "
            "Write a brief camp beat where the Keeper notices the item "
            "appearing in someone's hands."
        ),
    },
    {
        "id": "gate_full_party",
        "library_min": 4,
        "session_min": 3,
        "features": ["camp_rest", "release_sell"],
        "narration_hint": (
            "The full active party has assembled. Camp rest rolls and memento "
            "sinks (release/sell) are now available. Write a camp beat where "
            "the Keeper observes that the fire is finally surrounded — "
            "there's a party now, not just travelers."
        ),
    },
    {
        "id": "gate_camp_skills",
        "library_min": 5,
        "session_min": 12,
        "features": ["equip_slots", "camp_skills", "encounter_skills"],
        "narration_hint": (
            "The 5th persona has arrived and brought new capability. Equip "
            "slots (4 per persona), camp skills (a camp action that helps "
            "others), and encounter skills (once-per-session reactive "
            "abilities) are now available. Write a discovery beat where the "
            "Keeper notices that the newcomer brought something with them — "
            "a way of working the fire that the others hadn't tried."
        ),
    },
    {
        "id": "gate_introspect",
        "library_min": 6,
        "session_min": 25,
        "features": ["introspect"],
        "narration_hint": (
            "The 6th persona has arrived and with them, the Introspect action "
            "becomes available during camp. Instead of resting, a persona can "
            "turn inward — rolling a d20 on a banter table with high variance "
            "(the gamble of self-reflection). Write a discovery beat where "
            "someone at the fire looks down at their hands and says something "
            "that surprises even them. The Keeper frames this as a new kind "
            "of quiet."
        ),
    },
    {
        "id": "gate_full_roster",
        "library_min": 7,
        "session_min": 40,
        "features": ["full_roster"],
        "narration_hint": (
            "The final library slot has been filled. The full roster of 7 is "
            "assembled. Everything is live. The world pressure is biting. "
            "Endgame ceremonies are on the horizon. Write a brief, weighty "
            "observation — the Keeper sees the full circle and names what "
            "that means."
        ),
    },
]

GATE_BY_ID = {g["id"]: g for g in GATES}


# ── Arrival triggers ─────────────────────────────────────────────────────
# Early arrivals (1->4) are time/milestone-gated.
# Late arrivals (5->7) are struggle-gated.

EARLY_ARRIVAL_THRESHOLDS = [
    # (library_size_to_unlock, session_count_required)
    (2, 2),    # 2nd persona arrives at/after session 2
    (3, 3),    # 3rd at/after session 3
    (4, 5),    # 4th at/after session 5 (full party)
]

LATE_ARRIVAL_THRESHOLDS = [
    # (library_size_to_unlock, avg_stress_required, any_persona_stress_required)
    (5, 8,  12),   # 5th: avg stress >= 8 AND one persona >= 12
    (6, 10, 15),   # 6th: avg stress >= 10 AND one persona >= 15
    (7, 12, 18),   # 7th: avg stress >= 12 AND one persona >= 18
]


# ── Persistence ──────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "library_size":              0,
        "session_count":             0,
        "gates_fired":               [],
        "pending_unlock_narrations": [],
        "arrival_history":           [],
    }


def _load() -> dict:
    if not STORE_FILE.exists():
        return _empty_state()
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _empty_state().items():
            data.setdefault(k, v)
        return data
    except Exception:
        return _empty_state()


def _save(data: dict) -> None:
    tmp = STORE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(STORE_FILE)


# ── Public API ───────────────────────────────────────────────────────────

def get_state() -> dict:
    with _lock:
        return _load()


def get_library_size() -> int:
    with _lock:
        return int(_load().get("library_size", 1))


def get_session_count() -> int:
    with _lock:
        return int(_load().get("session_count", 0))


def increment_session_count() -> int:
    with _lock:
        data = _load()
        data["session_count"] = int(data.get("session_count", 0)) + 1
        _save(data)
        return data["session_count"]


def is_feature_unlocked(feature: str) -> bool:
    """Check if a specific feature is unlocked based on which gates have fired."""
    with _lock:
        data = _load()
    fired = set(data.get("gates_fired", []))
    for gate in GATES:
        if gate["id"] in fired and feature in gate.get("features", []):
            return True
    return False


def unlocked_features() -> set:
    """Return the full set of unlocked feature keys."""
    with _lock:
        data = _load()
    fired = set(data.get("gates_fired", []))
    features = set()
    for gate in GATES:
        if gate["id"] in fired:
            features.update(gate.get("features", []))
    return features


def check_gates() -> list[dict]:
    """Check all gate conditions against current state and fire any that
    are newly satisfiable. Returns the list of gates that just fired
    (empty if nothing new). Newly-fired gates are added to
    pending_unlock_narrations for the Keeper to narrate at the next camp.

    Called at session-end and camp-tick.
    """
    with _lock:
        data = _load()
        fired = set(data.get("gates_fired", []))
        lib_size = int(data.get("library_size", 1))
        sess_count = int(data.get("session_count", 0))
        newly_fired = []

        for gate in GATES:
            if gate["id"] in fired:
                continue
            # Check library and session minimums
            if lib_size < gate.get("library_min", 0):
                continue
            if sess_count < gate.get("session_min", 0):
                continue
            # Gate fires
            fired.add(gate["id"])
            data["gates_fired"] = list(fired)
            data.setdefault("pending_unlock_narrations", []).append(gate["id"])
            newly_fired.append(gate)

        if newly_fired:
            _save(data)
        return newly_fired


def pop_pending_narrations() -> list[dict]:
    """Return and clear any pending unlock narrations. Each entry is the
    full gate dict including its narration_hint. Called by the camp-tick
    handler to feed the Keeper her discovery beat prompts."""
    with _lock:
        data = _load()
        pending = list(data.get("pending_unlock_narrations", []))
        if not pending:
            return []
        data["pending_unlock_narrations"] = []
        _save(data)
    return [GATE_BY_ID[gid] for gid in pending if gid in GATE_BY_ID]


def record_arrival(persona_id: str) -> dict:
    """Record a persona arriving in the library. Increments library_size.
    Returns {library_size, persona_id, session}."""
    with _lock:
        data = _load()
        data["library_size"] = int(data.get("library_size", 1)) + 1
        entry = {
            "persona_id":  persona_id,
            "session":     int(data.get("session_count", 0)),
            "ts":          time.time(),
        }
        data["arrival_history"].append(entry)
        _save(data)
        return {
            "library_size": data["library_size"],
            "persona_id":   persona_id,
            "session":      entry["session"],
        }


def should_trigger_arrival() -> Optional[dict]:
    """Check if the next arrival should fire based on current state.
    Returns the threshold dict if an arrival should fire, or None.

    For early arrivals (library 1-4): checks session count.
    For late arrivals (library 5-7): checks struggle metrics from stress_store.
    """
    with _lock:
        data = _load()
    lib_size = int(data.get("library_size", 1))
    sess_count = int(data.get("session_count", 0))

    if lib_size >= LIBRARY_CAP:
        return None

    # Early arrivals (library 1->4): session-count gated
    # Late arrivals (library 5->7): struggle-gated
    # The boundary is at lib_size 4: once you have a full party of 4,
    # further arrivals require struggle.
    for target_size, sess_req in EARLY_ARRIVAL_THRESHOLDS:
        if lib_size < target_size and sess_count >= sess_req:
            return {"kind": "early", "target_size": target_size, "trigger": "session_count"}

    # If we're still in the early range but no threshold matched, wait
    max_early = max(t for t, _ in EARLY_ARRIVAL_THRESHOLDS)
    if lib_size < max_early:
        return None  # not enough sessions for the next early arrival

    # Late arrivals (lib 5-7) — struggle-gated
    # Once early arrivals are exhausted (lib >= max_early), further growth
    # requires the player to be suffering.
    try:
        import stress_store
        all_state = stress_store.get_all_state()
        stress_vals = [int(v) for v in (all_state.get("stress") or {}).values()
                       if not stress_store.is_departed(None)]  # rough filter
        # Filter out departed personas
        departed = set(stress_store.departed_personas())
        stress_vals = [int(v) for pid, v in (all_state.get("stress") or {}).items()
                       if pid not in departed]
        if not stress_vals:
            return None
        avg_stress = sum(stress_vals) / len(stress_vals)
        max_stress = max(stress_vals)

        for target_size, avg_req, any_req in LATE_ARRIVAL_THRESHOLDS:
            if lib_size < target_size and avg_stress >= avg_req and max_stress >= any_req:
                return {
                    "kind": "late",
                    "target_size": target_size,
                    "trigger": "struggle",
                    "avg_stress": avg_stress,
                    "max_stress": max_stress,
                }
    except Exception:
        pass

    return None
