"""
stress_store.py - Persistent per-persona stress and affliction state.

Stress accumulates during sessions (+3 per session, intensity-scaled later)
and drains while a persona rests in Camp. The Keeper is briefed with each
persona's current stress level when she opens a session so she knows who
is fragile, who is flush, who is about to crack.

Rolls during camp rest are d20-style, hidden. Critical successes generate
healing mementos (quiet, personal, forged from recovery rather than
conflict).

Schema of .persona_stress.json:
{
  "stress": {
    "<persona_id>": int,            # 0..MAX_STRESS, clamped
  },
  "afflictions": {
    "<persona_id>": "paranoid|fearful|..." | null
  },
  "virtues": {
    "<persona_id>": "stalwart|..."  | null  (rare, transformative)
  },
  "scars": {
    "<persona_id>": [ "scar_memento_id", ... ]
  },
  "last_roll_ts": {
    "<persona_id>": float,          # Unix timestamp of last camp rest roll
  },
  "rest_events": [                  # Ring buffer of recent heal/rise events
    { "ts", "persona_id", "kind", "narration", "memento_id?" }
  ]
}

All state is persisted to .persona_stress.json in the wingman dir.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Optional

WINGMAN_DIR = Path(__file__).parent
STORE_FILE  = WINGMAN_DIR / ".persona_stress.json"

# Stress scale - 0 is calm, MAX_STRESS is the hard ceiling where the
# Void ritual auto-fires. The brink lives at 20. Shattered at 25. Lost at 30.
#
# The player can refuse the ritual at BRINK_THRESHOLD and push past it. The
# persona keeps functioning but becomes a pressure source (spread stress to
# other party members). At SHATTERED_THRESHOLD spread damage doubles and
# new afflictions become permanent scars. At FORCE_VOID_THRESHOLD the next
# camp event auto-fires the Walk Into Void ritual - the game takes the
# decision back because there is no one left to decide for.
MAX_STRESS            = 30
BRINK_THRESHOLD       = 20
SHATTERED_THRESHOLD   = 25
FORCE_VOID_THRESHOLD  = 30

# The Becoming fires when a persona's combined resonance reaches this.
RESONANCE_THRESHOLD   = 20

# Resonance is split into two components that both count toward the
# threshold. The forged ratio at ceremony time determines which of four
# ceremony templates the Keeper writes.
#   >80% forged -> Fire-Tempered       (Severed Relic)
#   50-80%      -> Long Road            (Blessed Relic)
#   20-50%      -> Quiet Walk           (Faded Relic)
#   <20%        -> Comfortable End      (Comfort Relic)
RESONANCE_TIER_FIRE_TEMPERED     = 0.80
RESONANCE_TIER_LONG_ROAD         = 0.50
RESONANCE_TIER_QUIET_WALK        = 0.20

# Safe-resonance earning: 15 real-world minutes of conversation = +1.
# Daily cap prevents farming.
SAFE_RESONANCE_MINUTES_PER_POINT = 15
SAFE_RESONANCE_DAILY_CAP         = 3

# Camp roll cooldown - each persona can only roll this often
ROLL_COOLDOWN_SECONDS = 3600  # 1 hour

# Ring buffer of recent rest events
REST_EVENTS_MAX = 80

# Affliction / Virtue enums
AFFLICTIONS = ["paranoid", "fearful", "masochistic", "abusive", "irrational", "hopeless"]
VIRTUES     = ["stalwart", "vigorous", "focused", "courageous", "powerful"]

_lock = threading.RLock()


# ── Low-level IO ─────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "stress":          {},
        "afflictions":     {},
        "virtues":         {},
        "scars":           {},
        "last_roll_ts":    {},
        "rest_events":     [],
        "pending_edges":   {},   # {persona_id: [ {name, desc, source_event_ts, used: bool} ]}
        # Phase C additions:
        "resonance":       {},   # {persona_id: {safe: int, forged: int}}
        "safe_accrual":    {},   # {persona_id: {minutes_banked: float, day_ymd: "2026-04-11", points_today: int}}
        "brink":           {},   # {persona_id: {since_ts: float, status: "brink"|"shattered"|"lost"}}
        "departed":        {},   # {persona_id: {ts, kind: "becoming"|"void", ceremony_tier: str, relic_id: str}}
        "rescue_scars":    {},   # {persona_id: ["what_almost_happened", ...]}
        # Phase D/I additions:
        "world_pressure":  0,    # the losing-battle clock. Rises from rest/skip/sessions, drops from release/void/becoming
        "xp":              0,    # player-level XP. Earned from sessions, sells, future progression gates
    }

def _load() -> dict:
    if not STORE_FILE.exists():
        return _empty_state()
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Backfill all keys so old saves pick up the new schema cleanly
        empty = _empty_state()
        for k, v in empty.items():
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

def get_stress(persona_id: str) -> int:
    with _lock:
        data = _load()
        return int(data["stress"].get(persona_id, 0))


def get_affliction(persona_id: str) -> Optional[str]:
    with _lock:
        data = _load()
        return data["afflictions"].get(persona_id)


def get_virtue(persona_id: str) -> Optional[str]:
    with _lock:
        data = _load()
        return data["virtues"].get(persona_id)


def get_scars(persona_id: str) -> list[str]:
    with _lock:
        data = _load()
        return list(data["scars"].get(persona_id, []))


def get_all_state() -> dict:
    """Return a snapshot of everything - used by /party/state and session briefings."""
    with _lock:
        return _load()


def adjust_stress(persona_id: str, delta: int) -> int:
    """Add delta to a persona's stress. Clamps to [0, MAX_STRESS]. Returns new value."""
    with _lock:
        data = _load()
        current = int(data["stress"].get(persona_id, 0))
        new_val = max(0, min(MAX_STRESS, current + delta))
        data["stress"][persona_id] = new_val
        _save(data)
        return new_val


def set_stress(persona_id: str, value: int) -> int:
    """Set stress absolutely. Used by tests and admin routes."""
    with _lock:
        data = _load()
        new_val = max(0, min(MAX_STRESS, int(value)))
        data["stress"][persona_id] = new_val
        _save(data)
        return new_val


def set_affliction(persona_id: str, affliction: Optional[str]) -> None:
    """Set (or clear with None) a persona's current affliction."""
    with _lock:
        data = _load()
        if affliction is None:
            data["afflictions"].pop(persona_id, None)
        else:
            data["afflictions"][persona_id] = affliction
        _save(data)


def set_virtue(persona_id: str, virtue: Optional[str]) -> None:
    """Set (or clear) a persona's current virtue state."""
    with _lock:
        data = _load()
        if virtue is None:
            data["virtues"].pop(persona_id, None)
        else:
            data["virtues"][persona_id] = virtue
        _save(data)


def add_scar(persona_id: str, memento_id: str) -> None:
    """Pin a memento as a permanent scar on a persona."""
    with _lock:
        data = _load()
        scars = data["scars"].setdefault(persona_id, [])
        if memento_id not in scars:
            scars.append(memento_id)
        _save(data)


def can_roll_camp(persona_id: str) -> bool:
    """Whether this persona is past their camp roll cooldown."""
    with _lock:
        data = _load()
        last = float(data["last_roll_ts"].get(persona_id, 0))
        return (time.time() - last) >= ROLL_COOLDOWN_SECONDS


def time_until_next_roll(persona_id: str) -> float:
    """Seconds until this persona is eligible to roll again (0 if now)."""
    with _lock:
        data = _load()
        last = float(data["last_roll_ts"].get(persona_id, 0))
        remaining = ROLL_COOLDOWN_SECONDS - (time.time() - last)
        return max(0.0, remaining)


def _append_rest_event(event: dict) -> None:
    """Append to rest_events ring buffer. Caller must hold _lock and save."""
    data = _load()
    events = data.setdefault("rest_events", [])
    events.append(event)
    if len(events) > REST_EVENTS_MAX:
        data["rest_events"] = events[-REST_EVENTS_MAX:]
    _save(data)


def record_roll(
    persona_id: str,
    roll: int,
    stress_delta: int,
    kind: str,
    narration: str = "",
    memento_id: Optional[str] = None,
) -> dict:
    """Record a camp-roll outcome and update last_roll_ts. Returns the
    event dict for returning to the caller."""
    with _lock:
        data = _load()
        data["last_roll_ts"][persona_id] = time.time()
        # Adjust stress in-place
        current = int(data["stress"].get(persona_id, 0))
        new_val = max(0, min(MAX_STRESS, current + stress_delta))
        data["stress"][persona_id] = new_val
        event = {
            "ts":         time.time(),
            "persona_id": persona_id,
            "roll":       roll,
            "kind":       kind,          # "rest" | "healing_memento" | "virtue"
            "stress_before": current,
            "stress_after":  new_val,
            "narration":  narration,
        }
        if memento_id:
            event["memento_id"] = memento_id
        events = data.setdefault("rest_events", [])
        events.append(event)
        if len(events) > REST_EVENTS_MAX:
            data["rest_events"] = events[-REST_EVENTS_MAX:]
        _save(data)
        return event


def recent_events(n: int = 20) -> list[dict]:
    """Return the last N rest events, newest first."""
    with _lock:
        data = _load()
        events = list(data.get("rest_events", []))
        return list(reversed(events[-n:]))


def recent_events_for(persona_id: str, n: int = 5) -> list[dict]:
    """Return the last N rest events for a specific persona, newest first.
    Used by the Keeper's session briefing to know what each persona has
    been doing during their recent rest periods."""
    with _lock:
        data = _load()
        events = [e for e in data.get("rest_events", []) if e.get("persona_id") == persona_id]
        return list(reversed(events[-n:]))


def add_pending_edge(persona_id: str, name: str, desc: str) -> dict:
    """Add a temporary edge (single-use advantage) to a persona. Edges are
    usable only in the next session and consumed on use."""
    edge = {
        "name":    name,
        "desc":    desc,
        "created_ts": time.time(),
        "used":    False,
    }
    with _lock:
        data = _load()
        edges = data.setdefault("pending_edges", {}).setdefault(persona_id, [])
        edges.append(edge)
        _save(data)
    return edge


def get_pending_edges(persona_id: str) -> list[dict]:
    """Return unused pending edges for a persona."""
    with _lock:
        data = _load()
        edges = data.get("pending_edges", {}).get(persona_id, [])
        return [e for e in edges if not e.get("used")]


def clear_pending_edges(persona_id: str) -> int:
    """Mark all pending edges for a persona as used (called at session start
    so edges expire after one scene). Returns count cleared."""
    with _lock:
        data = _load()
        edges = data.get("pending_edges", {}).get(persona_id, [])
        cleared = 0
        for e in edges:
            if not e.get("used"):
                e["used"] = True
                cleared += 1
        _save(data)
        return cleared


def append_flavor_to_latest_event(persona_id: str, flavor: str) -> None:
    """Attach a flavor vignette to the most recent roll event for this
    persona. Used by the camp tick handler after the roll resolver runs."""
    with _lock:
        data = _load()
        events = data.get("rest_events", [])
        # Walk backwards to find the most recent matching event
        for ev in reversed(events):
            if ev.get("persona_id") == persona_id:
                ev["flavor"] = flavor
                break
        _save(data)


def append_edge_ref_to_latest_event(persona_id: str, edge: dict) -> None:
    """Attach an edge reference to the most recent roll event for this
    persona, so the event history preserves a link to the edge that was
    forged during that rest."""
    with _lock:
        data = _load()
        events = data.get("rest_events", [])
        for ev in reversed(events):
            if ev.get("persona_id") == persona_id:
                ev["edge"] = {"name": edge.get("name"), "desc": edge.get("desc")}
                break
        _save(data)


# ── Camp roll resolver ───────────────────────────────────────────────────

def roll_for_camp_rest(persona_id: str) -> dict:
    """Resolve one camp rest roll for a persona. Hidden d20; outcomes:

        1-10    : no heal
        11-17   : heal -1
        18-19   : heal -2
        20      : CRITICAL - heal -3 AND mints a healing memento

    Returns an event dict with at least:
        { "rolled", "kind", "stress_before", "stress_after", "critical": bool }

    Caller is responsible for actually forging the memento if critical=True
    (the memento forge lives in memento_forge.py). This function only
    reports that a critical occurred and records the roll.
    """
    if not can_roll_camp(persona_id):
        return {
            "eligible": False,
            "reason": "cooldown",
            "seconds_remaining": time_until_next_roll(persona_id),
        }
    roll = random.randint(1, 20)
    critical = (roll == 20)
    if roll <= 10:
        stress_delta = 0
        kind = "no_heal"
    elif roll <= 17:
        stress_delta = -1
        kind = "small_heal"
    elif roll <= 19:
        stress_delta = -2
        kind = "good_heal"
    else:  # 20
        stress_delta = -3
        kind = "critical_heal"
    event = record_roll(
        persona_id=persona_id,
        roll=roll,
        stress_delta=stress_delta,
        kind=kind,
    )
    event["eligible"] = True
    event["critical"] = critical
    return event


# ── Resonance (Phase C) ──────────────────────────────────────────────────
#
# Resonance is the positive mirror of stress. When it hits RESONANCE_THRESHOLD
# (20), The Becoming ritual unlocks. It has two components that count equally
# toward the threshold but differently toward the ceremony:
#
#   safe_resonance   - earned in Game Mode through real voice conversation
#   forged_resonance - earned in Campaign Mode (mementos, crit successes,
#                      surviving crit-fail scenes in affinity zones)
#
# The forged ratio at ceremony time picks one of four ceremony templates.

def get_resonance(persona_id: str) -> dict:
    """Return {safe, forged, total} for a persona."""
    with _lock:
        data = _load()
        entry = data["resonance"].get(persona_id, {}) or {}
        safe   = int(entry.get("safe", 0))
        forged = int(entry.get("forged", 0))
        return {"safe": safe, "forged": forged, "total": safe + forged}


def forged_ratio(persona_id: str) -> float:
    """Return forged / (safe + forged). 0.0 for pure safe, 1.0 for pure forged.
    Defaults to 0.5 (balanced) if the persona has zero resonance."""
    r = get_resonance(persona_id)
    total = r["total"]
    if total == 0:
        return 0.5
    return r["forged"] / total


def ceremony_tier_for(persona_id: str) -> str:
    """Pick the ceremony template for The Becoming based on forged ratio.
    Returns one of: fire_tempered | long_road | quiet_walk | comfortable_end."""
    ratio = forged_ratio(persona_id)
    if ratio >= RESONANCE_TIER_FIRE_TEMPERED:
        return "fire_tempered"
    if ratio >= RESONANCE_TIER_LONG_ROAD:
        return "long_road"
    if ratio >= RESONANCE_TIER_QUIET_WALK:
        return "quiet_walk"
    return "comfortable_end"


def _today_ymd() -> str:
    return time.strftime("%Y-%m-%d")


def add_safe_resonance(persona_id: str, minutes: float) -> dict:
    """Accrue safe resonance from real voice conversation minutes. Banks
    partial minutes, rounds up a point every SAFE_RESONANCE_MINUTES_PER_POINT,
    and caps at SAFE_RESONANCE_DAILY_CAP points per real-world day.

    Returns {minutes_added, points_gained, banked, points_today}.
    """
    with _lock:
        data = _load()
        today = _today_ymd()
        accrual = data["safe_accrual"].setdefault(persona_id, {
            "minutes_banked": 0.0,
            "day_ymd":         today,
            "points_today":    0,
        })
        # Reset daily counter on day rollover
        if accrual.get("day_ymd") != today:
            accrual["day_ymd"] = today
            accrual["points_today"] = 0

        accrual["minutes_banked"] = float(accrual.get("minutes_banked", 0.0)) + float(minutes)
        points_gained = 0
        # While we have enough banked minutes AND haven't hit the daily cap,
        # convert to points.
        while (
            accrual["minutes_banked"] >= SAFE_RESONANCE_MINUTES_PER_POINT
            and int(accrual["points_today"]) < SAFE_RESONANCE_DAILY_CAP
        ):
            accrual["minutes_banked"] -= SAFE_RESONANCE_MINUTES_PER_POINT
            accrual["points_today"]    = int(accrual["points_today"]) + 1
            points_gained += 1

        # Persist the point gain into the resonance store
        if points_gained > 0:
            entry = data["resonance"].setdefault(persona_id, {"safe": 0, "forged": 0})
            entry["safe"] = int(entry.get("safe", 0)) + points_gained

        _save(data)
        return {
            "minutes_added":  float(minutes),
            "points_gained":  points_gained,
            "banked":         accrual["minutes_banked"],
            "points_today":   int(accrual["points_today"]),
        }


def add_forged_resonance(persona_id: str, amount: int = 1, reason: str = "") -> int:
    """Award forged resonance to a persona. Called from session_forge when:
      - a memento is forged with them as owner   (+1)
      - they roll a crit_success in session      (+1)
      - they survive a crit_fail scene in their affinity zone  (+1)
    Returns new forged total."""
    with _lock:
        data = _load()
        entry = data["resonance"].setdefault(persona_id, {"safe": 0, "forged": 0})
        entry["forged"] = int(entry.get("forged", 0)) + int(amount)
        _save(data)
        return int(entry["forged"])


def is_ready_for_becoming(persona_id: str) -> bool:
    """True if total resonance >= RESONANCE_THRESHOLD and not yet departed."""
    if is_departed(persona_id):
        return False
    r = get_resonance(persona_id)
    return r["total"] >= RESONANCE_THRESHOLD


def personas_ready_for_becoming() -> list[str]:
    with _lock:
        data = _load()
    out = []
    for pid, entry in (data.get("resonance") or {}).items():
        if is_departed(pid):
            continue
        total = int(entry.get("safe", 0)) + int(entry.get("forged", 0))
        if total >= RESONANCE_THRESHOLD:
            out.append(pid)
    return out


# ── Brink state (Phase C) ────────────────────────────────────────────────
#
# When stress crosses BRINK_THRESHOLD the persona enters "brink" and the
# Walk Into Void ritual becomes available. The player can refuse and push
# them further - at SHATTERED_THRESHOLD they become a pressure source that
# spreads stress to the rest of the party. At FORCE_VOID_THRESHOLD the next
# camp event auto-fires the ritual.

def brink_status_for(persona_id: str) -> Optional[str]:
    """Return one of: 'brink' | 'shattered' | 'lost' | None."""
    s = get_stress(persona_id)
    if s >= FORCE_VOID_THRESHOLD:
        return "lost"
    if s >= SHATTERED_THRESHOLD:
        return "shattered"
    if s >= BRINK_THRESHOLD:
        return "brink"
    return None


def is_on_brink(persona_id: str) -> bool:
    return get_stress(persona_id) >= BRINK_THRESHOLD and not is_departed(persona_id)


def is_shattered(persona_id: str) -> bool:
    return get_stress(persona_id) >= SHATTERED_THRESHOLD and not is_departed(persona_id)


def should_force_void(persona_id: str) -> bool:
    return get_stress(persona_id) >= FORCE_VOID_THRESHOLD and not is_departed(persona_id)


def mark_brink_since(persona_id: str) -> None:
    """Record when a persona first crossed into brink. Idempotent."""
    with _lock:
        data = _load()
        if persona_id in data["brink"]:
            return
        data["brink"][persona_id] = {
            "since_ts": time.time(),
            "status":   "brink",
        }
        _save(data)


def personas_on_brink() -> list[str]:
    """Return all persona ids currently at stress >= BRINK_THRESHOLD
    (not yet departed). The UI uses this for portrait glow + ritual
    availability checks."""
    with _lock:
        data = _load()
    out = []
    for pid, s in (data.get("stress") or {}).items():
        if is_departed(pid):
            continue
        if int(s) >= BRINK_THRESHOLD:
            out.append(pid)
    return out


def brink_spread_damage_for(persona_id: str) -> int:
    """How much stress this persona spreads to each other active party
    member when they participate in a session. 0 below brink, +1 at brink,
    +2 at shattered, +3 at lost (though lost triggers forced ritual before
    this matters)."""
    s = get_stress(persona_id)
    if s >= FORCE_VOID_THRESHOLD:
        return 3
    if s >= SHATTERED_THRESHOLD:
        return 2
    if s >= BRINK_THRESHOLD:
        return 1
    return 0


def rescue_from_brink(persona_id: str) -> dict:
    """Called when a camp event or session outcome drops a brinked persona
    back below BRINK_THRESHOLD. Records the rescue scar and transitions
    them out of brink state.

    The rescue scar ('what_almost_happened') is permanent and unremovable.
    It grants both a small handicap (+1 DC on combat rolls) and a small
    virtue (half damage from future affliction triggers) - the mark of
    someone who almost left and came back changed.

    Returns the scar event dict for narration.
    """
    with _lock:
        data = _load()
        # Confirm they're actually rescued (stress now < brink)
        current = int(data["stress"].get(persona_id, 0))
        if current >= BRINK_THRESHOLD:
            return {"ok": False, "reason": "still_on_brink", "stress": current}
        # Remove from brink tracking
        data["brink"].pop(persona_id, None)
        # Add permanent rescue scar
        scars = data["rescue_scars"].setdefault(persona_id, [])
        scar_event = {
            "tag":       "what_almost_happened",
            "ts":        time.time(),
            "narration": "almost left us, and came back changed",
        }
        scars.append(scar_event)
        _save(data)
        return {"ok": True, "scar": scar_event, "persona_id": persona_id}


def has_rescue_scar(persona_id: str) -> bool:
    with _lock:
        data = _load()
        return len(data["rescue_scars"].get(persona_id, [])) > 0


# ── Departure state (Phase C) ────────────────────────────────────────────

def mark_departed(persona_id: str, kind: str, ceremony_tier: str = "", relic_id: str = "") -> None:
    """Flag a persona as permanently departed from this run. kind is
    'becoming' or 'void'. ceremony_tier is the template the Keeper used.
    relic_id is the uuid of the legacy relic produced by the ceremony."""
    with _lock:
        data = _load()
        data["departed"][persona_id] = {
            "ts":             time.time(),
            "kind":           kind,
            "ceremony_tier":  ceremony_tier or "",
            "relic_id":       relic_id or "",
        }
        # Clear stress/brink/resonance state - they no longer carry anything
        data["stress"].pop(persona_id, None)
        data["brink"].pop(persona_id, None)
        _save(data)


def is_departed(persona_id: str) -> bool:
    with _lock:
        data = _load()
        return persona_id in (data.get("departed") or {})


def departed_entry(persona_id: str) -> Optional[dict]:
    with _lock:
        data = _load()
        return dict(data["departed"].get(persona_id, {})) or None


def departed_personas() -> list[str]:
    with _lock:
        data = _load()
        return list((data.get("departed") or {}).keys())


# ── World Pressure (Phase D/I) ────────────────────────────────────────────
#
# A single counter tracking the losing battle. Rises from rest, skip, and
# session completion. Drops from Release (feeding dormant mementos to the
# world), Walk Into Void, and The Becoming. Phase I will add DC drift and
# encounter-type bias driven by this counter. For now it's a simple int
# with get/adjust helpers.

def get_world_pressure() -> int:
    with _lock:
        data = _load()
        return int(data.get("world_pressure", 0))


def adjust_world_pressure(delta: int) -> int:
    """Add delta to world_pressure (can be negative). Clamps to >= 0.
    Returns the new value."""
    with _lock:
        data = _load()
        current = int(data.get("world_pressure", 0))
        new_val = max(0, current + int(delta))
        data["world_pressure"] = new_val
        _save(data)
        return new_val


def world_pressure_dc_drift() -> int:
    """Compute the DC drift from world pressure using an asymptotic curve.
    Formula: 12 + 8 * (1 - e^(-pressure/30))
    Caps at DC 20. Gentle early, stabilizing late.

    This replaces the old linear formula (12 + pressure/4) which hit
    unplayable DCs at 40+ sessions.

    Curve shape:
      WP  0 -> DC 12    WP 15 -> DC 15    WP 30 -> DC 17
      WP 50 -> DC 18    WP 80 -> DC 19    WP 100+ -> DC 19-20 (cap)
    """
    import math
    wp = get_world_pressure()
    drift = int(8 * (1 - math.exp(-wp / 30)))
    return 12 + drift


# ── XP (Phase D) ─────────────────────────────────────────────────────────
#
# Player-level experience. Earned from sessions (flat +10 per completion),
# from selling dormant mementos, and from future progression milestones.
# What XP buys is defined by future phases — for now it just accumulates.

XP_PER_RARITY = {
    "common":    5,
    "uncommon":  10,
    "rare":      20,
    "epic":      40,
    "legendary": 80,
}


def get_xp() -> int:
    with _lock:
        data = _load()
        return int(data.get("xp", 0))


def add_xp(amount: int) -> int:
    """Award XP. Returns new total."""
    with _lock:
        data = _load()
        current = int(data.get("xp", 0))
        data["xp"] = current + max(0, int(amount))
        _save(data)
        return data["xp"]


# ── CLI inspection ───────────────────────────────────────────────────────

def _cli_show():
    data = _load()
    print(f"Store: {STORE_FILE}")
    print(f"Stress:")
    for pid, s in sorted(data["stress"].items()):
        afflicted = data["afflictions"].get(pid, "")
        virtue    = data["virtues"].get(pid, "")
        scars_n   = len(data["scars"].get(pid, []))
        extras = []
        if afflicted: extras.append(f"AFFL:{afflicted}")
        if virtue:    extras.append(f"VIRT:{virtue}")
        if scars_n:   extras.append(f"scars:{scars_n}")
        extras_str = "  " + " ".join(extras) if extras else ""
        print(f"  {pid:14s}  {s}/{MAX_STRESS}{extras_str}")
    print(f"Recent events ({len(data.get('rest_events', []))}):")
    for ev in reversed(data.get("rest_events", [])[-5:]):
        ts = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0)))
        print(f"  [{ts}] {ev.get('persona_id')}: rolled {ev.get('roll')}  "
              f"{ev.get('stress_before')}->{ev.get('stress_after')}  {ev.get('kind')}")


if __name__ == "__main__":
    _cli_show()
