"""
reward_cards.py - Post-session "pick 1 of 3" reward card system.

After every session, the player sees 3 cards drawn from a weighted pool.
Each card costs 0-N void crystals. The player picks 1 or skips all 3.
The cards are the primary spend target for void crystals and the core
moment-to-moment decision engine of the run.

Weighting is driven by current game state: stress levels, world pressure,
library size, departure history, memento inventory. Cards that solve the
player's current problem are MORE likely to appear — which means the
player is constantly choosing between immediate relief and long-term
investment.

Crystal sources:
  - Void departure:  +5
  - Becoming:        +3
  - 3 dormant released: +1
  - Introspect catastrophe: +1
  - XP conversion: 20 XP = 1 crystal (bad rate, desperation only)
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Optional

WINGMAN_DIR = Path(__file__).parent
STORE_FILE  = WINGMAN_DIR / ".void_crystals.json"

_lock = threading.RLock()


def _load() -> dict:
    if not STORE_FILE.exists():
        return {"crystals": 0, "lifetime_earned": 0, "lifetime_spent": 0,
                "card_history": [], "pending_cards": None}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("crystals", 0)
        data.setdefault("lifetime_earned", 0)
        data.setdefault("lifetime_spent", 0)
        data.setdefault("card_history", [])
        data.setdefault("pending_cards", None)
        return data
    except Exception:
        return {"crystals": 0, "lifetime_earned": 0, "lifetime_spent": 0,
                "card_history": [], "pending_cards": None}


def _save(data: dict) -> None:
    tmp = STORE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(STORE_FILE)


# ── Crystal management ──────────────────────────────────────────────────

def get_crystals() -> int:
    with _lock:
        return int(_load().get("crystals", 0))


def earn_crystals(amount: int, reason: str = "") -> int:
    """Award void crystals. Returns new total."""
    with _lock:
        data = _load()
        data["crystals"] = int(data.get("crystals", 0)) + max(0, int(amount))
        data["lifetime_earned"] = int(data.get("lifetime_earned", 0)) + max(0, int(amount))
        _save(data)
        return data["crystals"]


def spend_crystals(amount: int) -> dict:
    """Spend void crystals. Returns {ok, remaining} or {ok: False, error}."""
    with _lock:
        data = _load()
        current = int(data.get("crystals", 0))
        cost = max(0, int(amount))
        if cost > current:
            return {"ok": False, "error": f"not enough crystals ({current} < {cost})",
                    "crystals": current}
        data["crystals"] = current - cost
        data["lifetime_spent"] = int(data.get("lifetime_spent", 0)) + cost
        _save(data)
        return {"ok": True, "crystals": data["crystals"]}


def convert_xp_to_crystals(xp_amount: int) -> dict:
    """Convert XP to crystals at a bad rate (20 XP = 1 crystal).
    Returns {ok, crystals_gained, xp_spent, crystals_total}."""
    try:
        import stress_store
        current_xp = stress_store.get_xp()
        xp_to_spend = min(xp_amount, current_xp)
        crystals_gained = xp_to_spend // 20
        xp_actually_spent = crystals_gained * 20
        if crystals_gained <= 0:
            return {"ok": False, "error": f"need at least 20 XP (have {current_xp})"}
        # Deduct XP
        with _lock:
            sdata = stress_store._load()
            sdata["xp"] = max(0, int(sdata.get("xp", 0)) - xp_actually_spent)
            stress_store._save(sdata)
        new_total = earn_crystals(crystals_gained, reason="xp_conversion")
        return {"ok": True, "crystals_gained": crystals_gained,
                "xp_spent": xp_actually_spent, "crystals_total": new_total}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Card definitions ────────────────────────────────────────────────────

CARD_POOL = [
    {
        "id": "patch_up",
        "name": "Patch Up",
        "desc": "Heal 3 stress from one persona.",
        "cost": 0,
        "base_weight": 10,
        "effect": {"type": "heal_stress", "amount": 3, "targets": 1},
    },
    {
        "id": "edge",
        "name": "Edge",
        "desc": "One-use +3 bonus for next session's roll.",
        "cost": 0,
        "base_weight": 8,
        "effect": {"type": "grant_edge", "bonus": 3},
    },
    {
        "id": "window_extension",
        "name": "Window Extension",
        "desc": "Extend one memento's sidequest window by 3 sessions.",
        "cost": 1,
        "base_weight": 5,
        "effect": {"type": "extend_window", "amount": 3},
    },
    {
        "id": "memento_revival",
        "name": "Memento Revival",
        "desc": "Revive a dormant memento to pending with a fresh window.",
        "cost": 1,
        "base_weight": 4,
        "effect": {"type": "revive_memento"},
    },
    {
        "id": "resonance_spark",
        "name": "Resonance Spark",
        "desc": "+2 forged resonance to one persona.",
        "cost": 2,
        "base_weight": 4,
        "effect": {"type": "resonance_boost", "amount": 2},
    },
    {
        "id": "pressure_relief",
        "name": "Pressure Relief",
        "desc": "World pressure -5.",
        "cost": 2,
        "base_weight": 5,
        "effect": {"type": "reduce_pressure", "amount": 5},
    },
    {
        "id": "rare_forge",
        "name": "Rare Forge",
        "desc": "Guaranteed rare+ memento forged immediately.",
        "cost": 3,
        "base_weight": 3,
        "effect": {"type": "force_forge", "min_rarity": "rare"},
    },
    {
        "id": "skill_recharge",
        "name": "Skill Recharge",
        "desc": "Reset encounter skill for one persona (usable again next session).",
        "cost": 3,
        "base_weight": 3,
        "effect": {"type": "recharge_skill"},
    },
    {
        "id": "relic_awakening",
        "name": "Relic Awakening",
        "desc": "Upgrade a legacy relic to its stronger form.",
        "cost": 4,
        "base_weight": 2,
        "effect": {"type": "awaken_relic"},
    },
    {
        "id": "emergency_arrival",
        "name": "Emergency Arrival",
        "desc": "A new persona walks into camp. They arrive carrying +3 stress.",
        "cost": 5,
        "base_weight": 2,
        "effect": {"type": "trigger_arrival", "arrival_stress": 3},
    },
    {
        "id": "brink_rescue",
        "name": "Brink Rescue",
        "desc": "Drop one persona from brink (20+) to stress 10. No scar. Once per persona, ever.",
        "cost": 7,
        "base_weight": 1,
        "effect": {"type": "crystal_rescue", "target_stress": 10},
    },
    {
        "id": "timeline_fracture",
        "name": "Timeline Fracture",
        "desc": "Bring back a departed persona, weakened. Stats -2. Resonance at half rate. Their relic shatters.",
        "cost": 15,
        "base_weight": 0,  # only appears via state-weight boost
        "effect": {"type": "timeline_fracture"},
    },
    # ── Lifeline cards (only appear at small party sizes) ──
    {
        "id": "lone_wolf_resolve",
        "name": "Lone Wolf's Resolve",
        "desc": "The quiet of the empty camp is its own kind of strength. Heal 8 stress.",
        "cost": 0,
        "base_weight": 0,  # only via state-weight
        "effect": {"type": "heal_stress", "amount": 8, "targets": 1},
        "lifeline": True,
        "max_party": 2,
    },
    {
        "id": "defiance",
        "name": "Defiance",
        "desc": "The world expected you to fall. You did not. World pressure -8.",
        "cost": 0,
        "base_weight": 0,
        "effect": {"type": "reduce_pressure", "amount": 8},
        "lifeline": True,
        "max_party": 2,
    },
    {
        "id": "last_stand",
        "name": "The Last Stand",
        "desc": "Fewer shoulders. Harder arms. +4 to all rolls next session.",
        "cost": 0,
        "base_weight": 0,
        "effect": {"type": "party_buff_next", "bonus": 4},
        "lifeline": True,
        "max_party": 3,
    },
    {
        "id": "echo_of_fallen",
        "name": "Echo of the Fallen",
        "desc": "A departed companion's relic burns brighter. Double relic effect for 3 sessions.",
        "cost": 0,
        "base_weight": 0,
        "effect": {"type": "relic_amplify", "duration_sessions": 3},
        "lifeline": True,
        "max_party": 99,  # appears when departed > alive
        "requires_departed_majority": True,
    },
]

CARD_BY_ID = {c["id"]: c for c in CARD_POOL}


# ── Lone Wolf roll bonus ────────────────────────────────────────────────
# Applied in session_forge._roll_for_turn as an additive modifier.
# The fewer personas alive, the harder each survivor fights.
LONE_WOLF_BONUS = {1: 4, 2: 3, 3: 2}  # alive_count -> roll bonus


def get_lone_wolf_bonus() -> int:
    """Return the current lone wolf roll bonus based on alive party size."""
    try:
        import stress_store
        departed = set(stress_store.departed_personas())
        # Count non-departed personas across the whole library
        all_stress = stress_store.get_all_state().get("stress", {})
        alive = len([p for p in all_stress if p not in departed])
        return LONE_WOLF_BONUS.get(alive, 0)
    except Exception:
        return 0


def get_rest_drain_bonus() -> int:
    """Bonus stress drain per rest when party is small."""
    try:
        import stress_store
        departed = set(stress_store.departed_personas())
        all_stress = stress_store.get_all_state().get("stress", {})
        alive = len([p for p in all_stress if p not in departed])
        if alive <= 1: return 3
        if alive <= 2: return 2
        if alive <= 3: return 1
        return 0
    except Exception:
        return 0


def get_crystal_session_bonus() -> int:
    """Bonus crystals earned per session when party is small."""
    try:
        import stress_store
        departed = set(stress_store.departed_personas())
        all_stress = stress_store.get_all_state().get("stress", {})
        alive = len([p for p in all_stress if p not in departed])
        if alive <= 1: return 2
        if alive <= 3: return 1
        return 0
    except Exception:
        return 0


def _compute_card_weights(cards: list[dict]) -> list[float]:
    """Compute state-adjusted weights for the card pool. Cards that
    solve the player's current problem are weighted higher. Lifeline
    cards only activate at small party sizes."""
    weights = []
    # Read current state once
    try:
        import stress_store
        all_stress = stress_store.get_all_state().get("stress", {})
        departed = set(stress_store.departed_personas())
        alive_stress = {p: int(v) for p, v in all_stress.items() if p not in departed}
        alive_count = len(alive_stress)
        max_stress = max(alive_stress.values()) if alive_stress else 0
        avg_stress = (sum(alive_stress.values()) / len(alive_stress)) if alive_stress else 0
        wp = stress_store.get_world_pressure()
        has_departed = bool(departed)
        departed_count = len(departed)
        has_brink = max_stress >= 20
        crystals = get_crystals()
    except Exception:
        alive_stress = {}
        alive_count = 5
        max_stress = 0
        avg_stress = 0
        wp = 0
        has_departed = False
        departed_count = 0
        has_brink = False
        crystals = 0

    try:
        import memento_store
        dormant_count = len([m for m in memento_store.all_mementos()
                            if m.get("status") == "dormant"])
    except Exception:
        dormant_count = 0

    try:
        import game_progression
        lib_size = game_progression.get_library_size()
        lib_cap = game_progression.LIBRARY_CAP
    except Exception:
        lib_size = 5
        lib_cap = 7

    for card in cards:
        w = float(card.get("base_weight", 1))
        cid = card["id"]
        cost = card.get("cost", 0)

        # Lifeline gate: cards with max_party only appear when party is
        # small enough. If gate fails, weight is 0 (completely hidden).
        if card.get("lifeline"):
            max_party = card.get("max_party", 99)
            if card.get("requires_departed_majority"):
                if departed_count <= alive_count:
                    weights.append(0.0)
                    continue
                else:
                    w = 6.0  # strong weight when departed > alive
            elif alive_count > max_party:
                weights.append(0.0)
                continue
            else:
                # Lifeline card is eligible — give it strong weight
                w = 8.0 if alive_count <= 2 else 5.0

        # Can't afford it? Reduce weight (but don't zero — seeing expensive
        # cards you can't afford IS part of the agonizing)
        if cost > crystals:
            w *= 0.3

        # State-driven boosts
        if cid == "patch_up":
            if avg_stress > 10: w *= 1.5
            if max_stress > 20: w *= 2.0
        elif cid == "pressure_relief":
            if wp > 15: w *= 2.5
            elif wp > 8: w *= 1.5
        elif cid == "memento_revival":
            if dormant_count > 3: w *= 2.0
        elif cid == "emergency_arrival":
            if has_departed: w *= 3.0
            if lib_size < lib_cap and len(alive_stress) < 4: w *= 2.0
        elif cid == "brink_rescue":
            if has_brink: w *= 4.0
        elif cid == "timeline_fracture":
            if has_departed and crystals >= 10: w = 1.5
            elif has_departed: w = 0.3
            # else stays at 0 (base_weight)
        elif cid == "relic_awakening":
            if has_departed: w *= 2.0
        elif cid == "resonance_spark":
            # Boost when someone is close to Becoming threshold
            try:
                import stress_store as ss
                for pid in alive_stress:
                    r = ss.get_resonance(pid)
                    if 15 <= r["total"] < 20:
                        w *= 2.0
                        break
            except Exception:
                pass

        weights.append(max(w, 0.01))  # floor to prevent zero-weight
    return weights


def draw_cards(count: int = 3) -> list[dict]:
    """Draw `count` cards from the weighted pool without replacement.
    Stores the drawn cards as pending_cards so the player can pick one.
    Returns the list of card dicts with costs."""
    weights = _compute_card_weights(CARD_POOL)
    pool = list(CARD_POOL)

    drawn = []
    remaining_pool = list(zip(pool, weights))
    for _ in range(min(count, len(pool))):
        if not remaining_pool:
            break
        cards_left, weights_left = zip(*remaining_pool)
        picked = random.choices(range(len(cards_left)), weights=weights_left, k=1)[0]
        drawn.append(dict(cards_left[picked]))
        remaining_pool.pop(picked)

    # Store as pending
    with _lock:
        data = _load()
        data["pending_cards"] = {
            "cards": drawn,
            "drawn_ts": time.time(),
        }
        _save(data)

    return drawn


def get_pending_cards() -> Optional[list[dict]]:
    """Return the current pending card draw, or None if none pending."""
    with _lock:
        data = _load()
        pending = data.get("pending_cards")
        if not pending:
            return None
        return pending.get("cards")


def pick_card(card_id: str) -> dict:
    """Pick one of the pending cards. Spends the crystals. Clears pending.
    Returns {ok, card, crystals_remaining} or {ok: False, error}.

    NOTE: this function only handles the crystal spend and state cleanup.
    The EFFECT of the card is applied by the caller (session_forge or
    studio_server) based on the card's effect dict. This keeps the
    reward_cards module free of circular imports."""
    with _lock:
        data = _load()
        pending = data.get("pending_cards")
        if not pending:
            return {"ok": False, "error": "no pending cards"}
        cards = pending.get("cards", [])
        card = next((c for c in cards if c.get("id") == card_id), None)
        if not card:
            return {"ok": False, "error": f"card '{card_id}' not in current draw"}

    # Spend crystals
    cost = int(card.get("cost", 0))
    if cost > 0:
        result = spend_crystals(cost)
        if not result.get("ok"):
            return result

    # Record in history and clear pending
    with _lock:
        data = _load()
        data["card_history"].append({
            "card_id": card_id,
            "cost": cost,
            "ts": time.time(),
        })
        # Keep history bounded
        if len(data["card_history"]) > 200:
            data["card_history"] = data["card_history"][-200:]
        data["pending_cards"] = None
        _save(data)

    return {"ok": True, "card": card, "crystals_remaining": get_crystals()}


def skip_cards() -> dict:
    """Skip the current card draw without picking. Clears pending."""
    with _lock:
        data = _load()
        data["pending_cards"] = None
        _save(data)
    return {"ok": True, "action": "skipped"}


def get_economy_state() -> dict:
    """Full economy snapshot for the UI."""
    with _lock:
        data = _load()
    return {
        "crystals": int(data.get("crystals", 0)),
        "lifetime_earned": int(data.get("lifetime_earned", 0)),
        "lifetime_spent": int(data.get("lifetime_spent", 0)),
        "pending_cards": data.get("pending_cards"),
        "recent_picks": list(data.get("card_history", []))[-5:],
    }
