"""
camp_skills.py - Camp skill + encounter skill definitions and state.

Each persona has TWO skill slots chosen at creation from a pool of 3 options:
  - 1 camp skill:      used during camp instead of resting (helps others)
  - 1 encounter skill: used once per session mid-turn (reactive ability)

The choice is permanent — but Introspect miracle rolls (Phase K) can
unlock new options diegetically over the run.

Skills are gated: they only appear at library >= 5 (arrival of the 5th
persona brings the first choice-of-3 card).

Camp skills DON'T heal the user — they help the party. The tradeoff is:
rest heals you, skills help others, introspect gambles for everyone.

Encounter skills cost their "once per session" charge. Some rarity tiers
of mementos can extend the charge count.

Schema of .persona_skills.json:
{
  "skills": {
    "<persona_id>": {
      "camp_skill":      { "id": "reliquary_heart", "name": "Reliquary Heart", ... },
      "encounter_skill": { "id": "reframe",         "name": "Reframe", ... },
      "encounter_used":  false,    // reset at session start
    }
  }
}
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

WINGMAN_DIR = Path(__file__).parent
STORE_FILE  = WINGMAN_DIR / ".persona_skills.json"

_lock = threading.RLock()


def _load() -> dict:
    if not STORE_FILE.exists():
        return {"skills": {}}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("skills", {})
        return data
    except Exception:
        return {"skills": {}}


def _save(data: dict) -> None:
    tmp = STORE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(STORE_FILE)


# ── Skill definitions ────────────────────────────────────────────────────
# Each persona has 3 camp skill options + 3 encounter skill options.
# The player picks one of each at creation (arrival event).
# These are hand-authored for the 5 starters. The persona forge will
# generate options for new arrivals from their essence + inner weight.

CAMP_SKILL_OPTIONS: dict[str, list[dict]] = {
    "shadowheart": [
        {
            "id": "reliquary_heart",
            "name": "Reliquary Heart",
            "desc": "Heal 2 stress across 2 other personas. The quiet mending.",
            "effect": {"type": "heal_others", "targets": 2, "amount": 2},
        },
        {
            "id": "shadow_pact",
            "name": "Shadow Pact",
            "desc": "One ally gets +3 on their next stealth or emotional DC.",
            "effect": {"type": "buff_dc", "stat_types": ["stealth", "emotional"], "bonus": 3},
        },
        {
            "id": "quiet_hand",
            "name": "Quiet Hand",
            "desc": "Remove one affliction from an ally. Shadowheart takes +1 stress.",
            "effect": {"type": "cleanse", "self_cost": 1},
        },
    ],
    "laezel": [
        {
            "id": "iron_vigil",
            "name": "Iron Vigil",
            "desc": "Grant 3 stress armor to one ally. Absorbs the next stress hit.",
            "effect": {"type": "stress_armor", "targets": 1, "amount": 3},
        },
        {
            "id": "battle_drill",
            "name": "Battle Drill",
            "desc": "One ally gets +3 on their next combat DC.",
            "effect": {"type": "buff_dc", "stat_types": ["combat"], "bonus": 3},
        },
        {
            "id": "intercept",
            "name": "Intercept",
            "desc": "Absorb the next crit_fail stress hit for one ally. Laezel takes the hit instead.",
            "effect": {"type": "intercept_crit"},
        },
    ],
    "tesla": [
        {
            "id": "frequency_analysis",
            "name": "Frequency Analysis",
            "desc": "Reveal the next session's encounter type before it starts.",
            "effect": {"type": "forecast_encounter"},
        },
        {
            "id": "resonance_tuning",
            "name": "Resonance Tuning",
            "desc": "Convert the next encounter type to puzzle for one turn.",
            "effect": {"type": "convert_encounter", "to": "puzzle"},
        },
        {
            "id": "pressure_valve",
            "name": "Pressure Valve",
            "desc": "Reduce world pressure by 1 directly. Tesla sees the pattern and briefly unravels it.",
            "effect": {"type": "reduce_pressure", "amount": 1},
        },
    ],
    "jefferson": [
        {
            "id": "opening_address",
            "name": "Opening Address",
            "desc": "Convert the next encounter type to social for one turn.",
            "effect": {"type": "convert_encounter", "to": "social"},
        },
        {
            "id": "bond_deepening",
            "name": "Bond Deepening",
            "desc": "Two personas gain +1 forged resonance each. Words shape the bond.",
            "effect": {"type": "resonance_boost", "targets": 2, "amount": 1},
        },
        {
            "id": "memento_polish",
            "name": "Memento Polish",
            "desc": "Extend the sidequest window of one memento by 2 sessions.",
            "effect": {"type": "extend_window", "amount": 2},
        },
    ],
    "kerrigan": [
        {
            "id": "dominance_field",
            "name": "Dominance Field",
            "desc": "Reduce the next encounter DC by 2. The weak fold before the strong.",
            "effect": {"type": "reduce_dc", "amount": 2},
        },
        {
            "id": "survival_instinct",
            "name": "Survival Instinct",
            "desc": "Block the next affliction trigger for one ally. The wound lands but doesn't stick.",
            "effect": {"type": "block_affliction"},
        },
        {
            "id": "empty_slot_relief",
            "name": "Empty Slot Relief",
            "desc": "Advance the next persona arrival gate by -2 world pressure worth of relief.",
            "effect": {"type": "advance_arrival", "pressure_relief": 2},
        },
    ],
}

ENCOUNTER_SKILL_OPTIONS: dict[str, list[dict]] = {
    "shadowheart": [
        {
            "id": "reframe",
            "name": "Reframe",
            "desc": "Turn one crit_fail into a moderate_fail this session.",
            "effect": {"type": "downgrade_outcome", "from": "crit_fail", "to": "moderate_fail"},
        },
        {
            "id": "observe",
            "name": "Observe",
            "desc": "Reveal the encounter DC before the first roll.",
            "effect": {"type": "reveal_dc"},
        },
        {
            "id": "witness",
            "name": "Witness",
            "desc": "Once per session, take another persona's stress hit at +1.",
            "effect": {"type": "absorb_stress", "penalty": 1},
        },
    ],
    "laezel": [
        {
            "id": "press_attack",
            "name": "Press Attack",
            "desc": "Reroll one failed combat check this session.",
            "effect": {"type": "reroll", "encounter_types": ["combat"]},
        },
        {
            "id": "cover",
            "name": "Cover",
            "desc": "Take another persona's full stress hit this session.",
            "effect": {"type": "absorb_stress", "penalty": 0},
        },
        {
            "id": "war_cry",
            "name": "War Cry",
            "desc": "All party members get +1 on their next roll this session.",
            "effect": {"type": "party_buff", "bonus": 1},
        },
    ],
    "tesla": [
        {
            "id": "calculate",
            "name": "Calculate",
            "desc": "Reduce the encounter DC by 2 for one turn.",
            "effect": {"type": "reduce_dc_turn", "amount": 2},
        },
        {
            "id": "pattern_read",
            "name": "Pattern Read",
            "desc": "Reveal all remaining rolls' outcomes before they resolve.",
            "effect": {"type": "reveal_outcomes"},
        },
        {
            "id": "overcharge",
            "name": "Overcharge",
            "desc": "Add +4 to one roll, but Tesla gains +2 stress.",
            "effect": {"type": "boost_roll", "bonus": 4, "self_cost": 2},
        },
    ],
    "jefferson": [
        {
            "id": "parley",
            "name": "Parley",
            "desc": "Convert encounter type to social for one beat.",
            "effect": {"type": "convert_encounter_beat", "to": "social"},
        },
        {
            "id": "closing_argument",
            "name": "Closing Argument",
            "desc": "The persona in the CLOSES slot gets +3 on their roll.",
            "effect": {"type": "buff_closes", "bonus": 3},
        },
        {
            "id": "moral_weight",
            "name": "Moral Weight",
            "desc": "Double the forged resonance earned from this session's memento.",
            "effect": {"type": "double_resonance"},
        },
    ],
    "kerrigan": [
        {
            "id": "psionic_surge",
            "name": "Psionic Surge",
            "desc": "Force one NPC to comply. Kerrigan gains +3 stress.",
            "effect": {"type": "force_comply", "self_cost": 3},
        },
        {
            "id": "swarm_sense",
            "name": "Swarm Sense",
            "desc": "Negate one spread-stress event this session.",
            "effect": {"type": "block_spread"},
        },
        {
            "id": "apex_predator",
            "name": "Apex Predator",
            "desc": "If Kerrigan is in OPENS, her crit_success threshold drops to margin >= 8.",
            "effect": {"type": "lower_crit_threshold", "position": "opens", "new_threshold": 8},
        },
    ],
}


# ── State management ─────────────────────────────────────────────────────

def get_skills(persona_id: str) -> Optional[dict]:
    """Return the chosen skills for a persona, or None if not yet chosen."""
    with _lock:
        data = _load()
        return data["skills"].get(persona_id)


def get_all_skills() -> dict:
    """Return all chosen skills across all personas."""
    with _lock:
        return dict(_load().get("skills", {}))


def choose_skills(persona_id: str, camp_skill_id: str, encounter_skill_id: str) -> dict:
    """Lock in a persona's skill choices. Called at the arrival ceremony
    when the player picks from the choice-of-3 card.

    Returns {ok, camp_skill, encounter_skill} or {ok: False, error}."""
    camp_options = CAMP_SKILL_OPTIONS.get(persona_id, [])
    enc_options = ENCOUNTER_SKILL_OPTIONS.get(persona_id, [])

    camp_pick = next((s for s in camp_options if s["id"] == camp_skill_id), None)
    enc_pick  = next((s for s in enc_options if s["id"] == encounter_skill_id), None)

    if not camp_pick:
        return {"ok": False, "error": f"invalid camp skill: {camp_skill_id}"}
    if not enc_pick:
        return {"ok": False, "error": f"invalid encounter skill: {encounter_skill_id}"}

    entry = {
        "camp_skill":      dict(camp_pick),
        "encounter_skill": dict(enc_pick),
        "encounter_used":  False,
        "chosen_ts":       time.time(),
    }
    with _lock:
        data = _load()
        data["skills"][persona_id] = entry
        _save(data)
    return {"ok": True, "camp_skill": camp_pick, "encounter_skill": enc_pick}


def reset_encounter_skill(persona_id: str) -> None:
    """Reset the encounter skill charge for a new session."""
    with _lock:
        data = _load()
        entry = data["skills"].get(persona_id)
        if entry:
            entry["encounter_used"] = False
            _save(data)


def use_encounter_skill(persona_id: str) -> dict:
    """Mark the encounter skill as used. Returns the skill dict if
    successful, or {ok: False, error} if already used or not chosen."""
    with _lock:
        data = _load()
        entry = data["skills"].get(persona_id)
        if not entry:
            return {"ok": False, "error": "no skills chosen"}
        if entry.get("encounter_used"):
            return {"ok": False, "error": "already used this session"}
        entry["encounter_used"] = True
        _save(data)
        return {"ok": True, "skill": entry.get("encounter_skill")}


def get_camp_skill_options(persona_id: str) -> list[dict]:
    """Return the 3 camp skill options for a persona (for the choice card)."""
    return list(CAMP_SKILL_OPTIONS.get(persona_id, []))


def get_encounter_skill_options(persona_id: str) -> list[dict]:
    """Return the 3 encounter skill options for a persona."""
    return list(ENCOUNTER_SKILL_OPTIONS.get(persona_id, []))


def has_chosen_skills(persona_id: str) -> bool:
    with _lock:
        data = _load()
        return persona_id in data.get("skills", {})


# ── Introspect (Phase K) ────────────────────────────────────────────────
# The d20 banter table. Sit out = gamble. EV slightly negative.
# Dynamic tooltip changes based on scenario state to entice the player.

import random

INTROSPECT_TABLE = [
    # (roll_range, tier, label)
    # EV slightly negative: 35% bad / 35% neutral / 30% good (DD-pure).
    # Bad outcomes are more FREQUENT but good outcomes are bigger in IMPACT.
    ((1, 1),   "catastrophe",  "Something dark surfaced."),
    ((2, 7),   "bad",          "Something shifted, and not well."),
    ((8, 14),  "neutral",      "A scene. A memory. Nothing changed."),
    ((15, 17), "good",         "Something eased."),
    ((18, 19), "miracle",      "Something true surfaced."),
    ((20, 20), "jackpot",      "Something extraordinary."),
]

# Tier -> mechanical outcomes (applied by the caller, not by introspect itself)
INTROSPECT_OUTCOMES = {
    "catastrophe": {
        "stress_self": 4,
        "stress_others": 1,    # each other party member
        "world_pressure": 3,
        "desc": "+4 stress to self, +1 to each ally, world pressure +3",
    },
    "bad": {
        "stress_self": 2,
        "stress_others": 0,
        "world_pressure": 1,
        "desc": "+2 stress to self, world pressure +1",
    },
    "neutral": {
        "stress_self": 0,
        "stress_others": 0,
        "world_pressure": 0,
        "desc": "A scene plays. No mechanical change.",
    },
    "good": {
        "stress_self": -2,
        "stress_others": -1,   # each other party member
        "world_pressure": -1,
        "desc": "-2 stress to self, -1 to each ally, world pressure -1",
    },
    "miracle": {
        "stress_self": -4,
        "stress_others": -1,
        "world_pressure": -2,
        "desc": "-4 stress to self, -1 to each ally, world pressure -2. May unlock a new skill option or revive a dormant memento.",
        "bonus": "skill_unlock_or_memento_revive",
    },
    "jackpot": {
        "stress_self": -6,
        "stress_others": -2,
        "world_pressure": -4,
        "desc": "Mass heal. Next arrival gate advanced. If anyone is at brink, drop them to stress 15.",
        "bonus": "mass_heal_and_arrival",
    },
}

# Dynamic tooltips for the Introspect button. The tooltip changes based
# on the current game state to entice the player without explaining the
# mechanic. Each is a (condition_fn, tooltip_text) pair; the first match
# wins.
INTROSPECT_TOOLTIPS = [
    # These are checked in order. condition is a function(stress_store_state) -> bool.
    # For now we use static strings keyed by state checks at the server level.
    ("high_stress",     "We are running out of air."),
    ("low_stress",      "Did something change?"),
    ("high_pressure",   "The tide is coming. Listen."),
    ("post_crit_fail",  "I need to understand what just happened."),
    ("post_crit_success", "Was that us? Was that real?"),
    ("never_used",      "There is something here you haven't tried."),
    ("default",         "Something... anything... help us..."),
]


def roll_introspect() -> dict:
    """Roll the d20 introspect table. Returns {roll, tier, label, outcomes}."""
    roll = random.randint(1, 20)
    tier = "neutral"
    label = ""
    for (lo, hi), t, l in INTROSPECT_TABLE:
        if lo <= roll <= hi:
            tier = t
            label = l
            break
    outcomes = dict(INTROSPECT_OUTCOMES.get(tier, INTROSPECT_OUTCOMES["neutral"]))
    return {
        "roll": roll,
        "tier": tier,
        "label": label,
        "outcomes": outcomes,
    }


def get_introspect_tooltip(state_hint: str = "default") -> str:
    """Return the dynamic tooltip for the Introspect button based on
    the current game state hint."""
    for hint, text in INTROSPECT_TOOLTIPS:
        if hint == state_hint:
            return text
    return INTROSPECT_TOOLTIPS[-1][1]  # default
