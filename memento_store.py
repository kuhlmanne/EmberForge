"""
memento_store.py - Persistent store for relationship mementos.

Every time two personas share a completed exchange, memento_forge.py mints
a single item that "emerged" from that moment. The item sits in BOTH personas'
inventories (they share the memory of the moment) and references the other as
its source. The full inventory of a persona is therefore a physical expression
of every interaction they've been part of.

Schema of .persona_mementos.json:
{
  "mementos": [
    {
      "id":           "<uuid>",
      "owners":       ["laezel", "shadowheart"],  # both personas present
      "pair_key":     "laezel+shadowheart",        # canonical sorted
      "kind":         "converse",                  # exchange kind (argue, agree, banter...)
      "name":         "A silver pendant, slightly warmed",
      "desc":         "Laezel pressed it into Shadowheart's palm without comment. Neither spoke of it again.",
      "rarity":       "rare",
      "arc_summary":  "<the arc summary that produced this memento>",
      "created_ts":   1713009876.0
    }
  ]
}

API (thread-safe):
    add_memento(memento_dict) -> None
    all_mementos()            -> list[dict]
    mementos_for(persona_id)  -> list[dict]       (sorted, newest first)
    mementos_for_pair(a, b)   -> list[dict]       (sorted, newest first)
    forget_persona(id)        -> None             (removes all mementos owned by id)
    has_memento_for_exchange(pair_key, created_after_ts=0) -> bool
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

WINGMAN_DIR = Path(__file__).parent
STORE_FILE  = WINGMAN_DIR / ".persona_mementos.json"

_lock = threading.RLock()


def _pair_key(a: str, b: str) -> str:
    lo, hi = sorted([a, b])
    return f"{lo}+{hi}"


def _load() -> dict:
    if not STORE_FILE.exists():
        return {"mementos": []}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("mementos", [])
        return data
    except Exception:
        return {"mementos": []}


def _save(data: dict) -> None:
    tmp = STORE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(STORE_FILE)


def add_memento(m: dict) -> dict:
    """Append a memento. Fills in id/created_ts/pair_key if missing.
    Returns the stored entry."""
    m = dict(m)
    m.setdefault("id", uuid.uuid4().hex[:12])
    m.setdefault("created_ts", time.time())
    owners = m.get("owners") or []
    if len(owners) == 2 and "pair_key" not in m:
        m["pair_key"] = _pair_key(owners[0], owners[1])
    m.setdefault("rarity", "uncommon")
    with _lock:
        data = _load()
        data["mementos"].append(m)
        _save(data)
    return m


def all_mementos() -> list[dict]:
    with _lock:
        data = _load()
        return list(data["mementos"])


def mementos_for(persona_id: str) -> list[dict]:
    """All mementos in which this persona is an owner, newest first."""
    result = [m for m in all_mementos() if persona_id in (m.get("owners") or [])]
    result.sort(key=lambda m: m.get("created_ts", 0), reverse=True)
    return result


def mementos_for_pair(a: str, b: str) -> list[dict]:
    key = _pair_key(a, b)
    result = [m for m in all_mementos() if m.get("pair_key") == key]
    result.sort(key=lambda m: m.get("created_ts", 0), reverse=True)
    return result


def has_memento_for_exchange(pair_key: str, created_after_ts: float = 0) -> bool:
    """Check if a memento exists for a pair produced after a given timestamp.
    Used to avoid double-forging for the same arc summary."""
    for m in all_mementos():
        if m.get("pair_key") == pair_key and m.get("created_ts", 0) > created_after_ts:
            return True
    return False


def forget_persona(persona_id: str) -> None:
    """Drop all mementos involving this persona (paired with forget in interaction_store)."""
    with _lock:
        data = _load()
        data["mementos"] = [m for m in data["mementos"]
                             if persona_id not in (m.get("owners") or [])]
        _save(data)


# ── Phase L: Sidequests + lifespans ────────────────────────────────────
#
# Mementos are no longer immortal. Every memento is forged with a one-line
# sidequest - a condition under which it wants to be spent in a scene.
# Each session it's "live" (conditions matchable in the current encounter)
# that does NOT fulfill it, the window ticks down. When the window expires,
# the memento transitions to DORMANT: still in inventory, still visible,
# but silent - no stat bonuses, no sidequest. When the Keeper writes a
# scene that references it by name or calls back its arc_summary, the
# memento is HONORED: archived to the memorial, awarded forged resonance
# to both owners, removed from the live inventory.
#
# Three statuses:
#   pending  — default, active, can still fire
#   honored  — spent in a scene; archived to memorial, out of live inventory
#   dormant  — window expired; still in inventory but silent
#
# Relics (kind=legacy) never have sidequests — they ARE the spent form.

# Rarity -> number of sessions the sidequest stays live before going dormant.
# Common memories fade fast. Legendaries wait forever.
SIDEQUEST_WINDOW_BY_RARITY = {
    "common":    3,
    "uncommon":  5,
    "rare":      8,
    "epic":     12,
    "legendary": 9999,   # effectively infinite
}


def default_window_for_rarity(rarity: str) -> int:
    return int(SIDEQUEST_WINDOW_BY_RARITY.get((rarity or "uncommon").lower(), 5))


def get_memento(memento_id: str) -> Optional[dict]:
    with _lock:
        data = _load()
        for m in data["mementos"]:
            if m.get("id") == memento_id:
                return dict(m)
    return None


def _update_memento(memento_id: str, updater) -> Optional[dict]:
    """Atomically locate, update, and persist a single memento. updater is
    called with the live dict (mutate in place). Returns the updated dict
    or None if not found."""
    with _lock:
        data = _load()
        for m in data["mementos"]:
            if m.get("id") == memento_id:
                updater(m)
                _save(data)
                return dict(m)
    return None


def set_memento_status(memento_id: str, status: str) -> Optional[dict]:
    """Transition a memento between pending / honored / dormant."""
    def _u(m):
        m["status"] = status
        if "sidequest" in m and isinstance(m["sidequest"], dict):
            m["sidequest"]["status"] = status
    return _update_memento(memento_id, _u)


def tick_sidequest_window(memento_id: str) -> Optional[dict]:
    """Bump window_used by 1. If window_used >= window_total, transition
    to dormant. Returns the updated memento (or None if not found)."""
    def _u(m):
        sq = m.get("sidequest") or {}
        used = int(sq.get("window_used", 0)) + 1
        total = int(sq.get("window_total", 0))
        sq["window_used"] = used
        if total > 0 and used >= total:
            m["status"]  = "dormant"
            sq["status"] = "dormant"
        m["sidequest"] = sq
    return _update_memento(memento_id, _u)


def honor_memento(memento_id: str, session_id: str = "") -> Optional[dict]:
    """Mark a memento as honored (spent in a scene). Sets status + timestamps.
    Does NOT move it between lists - it stays in the main mementos list but
    queries can filter by status."""
    def _u(m):
        m["status"] = "honored"
        m["honored_ts"] = time.time()
        m["honored_in"] = session_id or ""
        sq = m.get("sidequest") or {}
        sq["status"] = "honored"
        m["sidequest"] = sq
    return _update_memento(memento_id, _u)


# ── Status-filtered queries ─────────────────────────────────────────────

def _filter_by_status(memento: dict, wanted: str) -> bool:
    """Return True if this memento matches the wanted status. Legacy relics
    (kind='legacy') are always included in 'active' queries regardless of
    sidequest status — they ARE the spent form."""
    if memento.get("kind") == "legacy":
        return wanted == "active"
    status = (memento.get("status") or "pending")
    if wanted == "active":
        # Active = pending + dormant (anything still in live inventory)
        return status in ("pending", "dormant")
    if wanted == "pending":
        return status == "pending"
    if wanted == "dormant":
        return status == "dormant"
    if wanted == "honored":
        return status == "honored"
    return True


def active_mementos_for(persona_id: str) -> list[dict]:
    """Everything a persona has in their live inventory (pending + dormant
    + legacy relics). Honored mementos are NOT returned — they live in the
    memorial wall."""
    result = [m for m in all_mementos()
              if persona_id in (m.get("owners") or [])
              and _filter_by_status(m, "active")]
    result.sort(key=lambda m: m.get("created_ts", 0), reverse=True)
    return result


def pending_mementos_for(persona_id: str) -> list[dict]:
    """Only mementos that could still fire their sidequest."""
    result = [m for m in all_mementos()
              if persona_id in (m.get("owners") or [])
              and _filter_by_status(m, "pending")]
    result.sort(key=lambda m: m.get("created_ts", 0), reverse=True)
    return result


def dormant_mementos_for(persona_id: str) -> list[dict]:
    """Silent mementos — their window expired but they're still carried."""
    result = [m for m in all_mementos()
              if persona_id in (m.get("owners") or [])
              and _filter_by_status(m, "dormant")]
    result.sort(key=lambda m: m.get("created_ts", 0), reverse=True)
    return result


def memorial_mementos_for(persona_id: Optional[str] = None) -> list[dict]:
    """All honored mementos. If persona_id is given, only those where that
    persona was an owner. Sorted by honored_ts descending."""
    result = [m for m in all_mementos() if _filter_by_status(m, "honored")]
    if persona_id:
        result = [m for m in result if persona_id in (m.get("owners") or [])]
    result.sort(key=lambda m: m.get("honored_ts", m.get("created_ts", 0)), reverse=True)
    return result


def live_sidequests_for_scene(party_ids: list[str],
                               encounter_type: str,
                               turn_order: list[str]) -> list[dict]:
    """Return all pending-status mementos whose sidequest conditions are
    satisfiable by the current scene. Called at session start and also on
    each per-turn roll to find surgical briefings for the Keeper.

    A sidequest is LIVE if:
      - the memento is EQUIPPED by at least one owner in the party (hot/cold gate)
      - all owners_in_party are in the active party
      - encounter_type matches (or sidequest encounter_type is 'any')
      - position requirements are met by the turn_order

    Unequipped mementos are "cold" — still carried but their sidequest
    won't fire until equipped. Equipment slots are gated at library >= 5
    (Phase H); before that gate opens, ALL pending mementos are treated
    as equipped (since the player has no way to unequip them).
    """
    party_set = set(party_ids or [])
    live = []
    # Build a set of all equipped memento IDs across the party for the
    # hot/cold gate. Pre-gate (before equip slots unlock), we treat
    # everything as equipped.
    _equipped_ids = set()
    _equip_gated = False
    try:
        eq_data = _load().get("equipment") or {}
        # If ANY persona has equipment data, the gate is open
        _equip_gated = bool(eq_data)
        for pid in party_ids or []:
            for mid in (eq_data.get(pid) or []):
                if mid:
                    _equipped_ids.add(mid)
    except Exception:
        pass

    for m in all_mementos():
        if not _filter_by_status(m, "pending"):
            continue
        # Hot/cold gate: if equip system is active, memento must be equipped
        if _equip_gated and m.get("id") not in _equipped_ids:
            continue
        sq = m.get("sidequest") or {}
        required = sq.get("required") or {}
        if not required:
            continue
        # Owners in party check
        needed_owners = required.get("owners_in_party") or []
        if any(o not in party_set for o in needed_owners):
            continue
        # Encounter type check
        req_enc = (required.get("encounter_type") or "any").lower()
        if req_enc != "any" and req_enc != (encounter_type or "").lower():
            continue
        # Position check
        position_req = required.get("position") or {}
        position_ok = True
        for pid, slot_name in position_req.items():
            slot_name = (slot_name or "any").lower()
            if slot_name == "any":
                continue
            if pid not in turn_order:
                position_ok = False
                break
            idx = turn_order.index(pid)
            if slot_name == "opens" and idx != 0:
                position_ok = False; break
            if slot_name == "closes" and idx != len(turn_order) - 1:
                position_ok = False; break
            if slot_name == "middle" and (idx == 0 or idx == len(turn_order) - 1):
                position_ok = False; break
        if not position_ok:
            continue
        live.append(m)
    return live


# ── Phase C: Relic tier (legacy items from persona departure) ──────────
#
# When a persona departs (Becoming or Void), two things happen:
#   1. A single new "legacy relic" is minted, bound to the departed persona
#      as its sole "relic_persona" source. This is a one-per-persona item.
#   2. Every pre-existing memento owned by the departed persona is
#      transformed in-place - stamped with "transformed_from" and a
#      "legacy_kind" of "blessed" (becoming) or "lost" (void). The original
#      stats and abilities are preserved; the legacy_kind is a tonal flag
#      the UI reads for tinting and a modifier the build system reads for
#      the small camp bonus / tithe attached to each transformed memento.

# Rarity ordering including the relic tiers - used for sorting and for
# floors in the forge pair-exchange logic.
RARITY_ORDER = [
    "common", "uncommon", "rare", "epic", "legendary",
    "comfort_relic", "faded_relic", "blessed_relic",
    "severed_relic",                # fire_tempered unique
    "lost_relic",                   # always-full-weight void unique
]


# Blessed ceremony tier -> relic rarity mapping
BECOMING_TIER_TO_RARITY = {
    "fire_tempered":    "severed_relic",
    "long_road":        "blessed_relic",
    "quiet_walk":       "faded_relic",
    "comfortable_end":  "comfort_relic",
}


def mint_legacy_relic(
    persona_id: str,
    origin: str,                 # "becoming" | "void"
    ceremony_tier: str,          # "fire_tempered"|"long_road"|"quiet_walk"|"comfortable_end"|"dd_full"
    name: str,
    desc: str,
    stats: Optional[dict] = None,
    abilities: Optional[list] = None,
    flavor_tag: str = "",
    arc_summary: str = "",
) -> dict:
    """Mint a brand-new legacy relic bound to a departing persona. This is
    the one-of-one legacy item produced by the ceremony itself (separate
    from the mementos that transform). Persisted into memento_store with
    a special 'legacy' kind and 'relic_persona' field that the UI uses to
    render it in the memorial.
    """
    if origin == "becoming":
        rarity = BECOMING_TIER_TO_RARITY.get(ceremony_tier, "blessed_relic")
    else:
        # Void ceremonies always produce a lost_relic (always full weight)
        rarity = "lost_relic"

    relic = {
        "id":            uuid.uuid4().hex[:12],
        "owners":        [persona_id],
        "pair_key":      f"{persona_id}+{persona_id}",
        "kind":          "legacy",
        "name":          str(name)[:80],
        "desc":          str(desc)[:500],
        "rarity":        rarity,
        "slot":          "relic",
        "stats":         dict(stats or {}),
        "abilities":     [str(a)[:160] for a in (abilities or [])][:3],
        "flavor_tag":    str(flavor_tag)[:120],
        "arc_summary":   str(arc_summary)[:400],
        "source":        "ceremony",
        "origin":        origin,            # becoming | void
        "ceremony_tier": ceremony_tier,
        "relic_persona": persona_id,        # binds this relic to the departed
        "created_ts":    time.time(),
        # Relics cannot be traded/sold/gifted - this flag is enforced by
        # Phase D (memento sinks).
        "locked":        True,
    }
    with _lock:
        data = _load()
        data["mementos"].append(relic)
        _save(data)
    return relic


def transform_persona_mementos(persona_id: str, legacy_kind: str) -> int:
    """In-place transform every memento in which this persona is an owner
    to the legacy form. legacy_kind is 'blessed' (becoming) or 'lost' (void).

    Transformed mementos retain their name/desc/stats but gain a
    'legacy_kind' flag the UI reads for tint, a 'transformed_from' (the
    original rarity), and a 'transformed_ts'. A small camp bonus / tithe
    is encoded for Phase D/E to read.

    Returns the count of transformed mementos.
    """
    count = 0
    with _lock:
        data = _load()
        for m in data["mementos"]:
            if persona_id not in (m.get("owners") or []):
                continue
            if m.get("kind") == "legacy":
                # Don't transform legacy relics - they're already terminal
                continue
            if m.get("legacy_kind"):
                # Already transformed (idempotent)
                continue
            m["legacy_kind"]      = legacy_kind                 # blessed | lost
            m["transformed_from"] = m.get("rarity", "uncommon")
            m["transformed_ts"]   = time.time()
            if legacy_kind == "blessed":
                # Small passive camp bonus: +0.25 stress drain per session
                m["legacy_bonus"]  = {"type": "stress_drain", "value": 0.25}
            else:
                # Small tithe: equipping adds +1 stress per session
                m["legacy_bonus"]  = {"type": "stress_tithe",  "value": 1}
            count += 1
        _save(data)
    return count


def legacy_relics() -> list[dict]:
    """Return every relic produced by ceremony (both becoming and void),
    newest first. Used by the memorial UI."""
    result = [m for m in all_mementos() if m.get("kind") == "legacy"]
    result.sort(key=lambda m: m.get("created_ts", 0), reverse=True)
    return result


def legacy_relic_for(persona_id: str) -> Optional[dict]:
    """Return the one legacy relic bound to this persona, if they've
    departed. None if they haven't."""
    for m in legacy_relics():
        if m.get("relic_persona") == persona_id:
            return m
    return None


# ── CLI inspection ─────────────────────────────────────────────────────────

# ── Phase D: Memento sinks (Release + Sell) ────────────────────────────
#
# Dormant mementos aren't garbage — they're fuel. Two verbs turn them
# into resources that counterplay the losing battle:
#
#   Release — feed the world an expired memory, world pressure drops
#   Sell    — trade an expired memory for XP
#
# Both remove the memento from inventory. Both only work on dormant or
# pending mementos (not legacy relics, not honored ones). New verbs
# will be added as the game reveals what it needs.


def _remove_memento(memento_id: str) -> Optional[dict]:
    """Remove a memento by id from the store. Returns the removed dict
    or None if not found. Used by sink verbs."""
    with _lock:
        data = _load()
        removed = None
        remaining = []
        for m in data["mementos"]:
            if m.get("id") == memento_id and removed is None:
                removed = m
            else:
                remaining.append(m)
        if removed:
            data["mementos"] = remaining
            _save(data)
        return removed


def release_memento(memento_id: str) -> dict:
    """Release a dormant/pending memento to reduce world pressure. The
    memento is removed from inventory and the world exhales for a moment.

    Returns {ok, pressure_before, pressure_after, memento_name}."""
    m = get_memento(memento_id)
    if not m:
        return {"ok": False, "error": "memento not found"}
    if m.get("kind") == "legacy":
        return {"ok": False, "error": "legacy relics cannot be released"}
    if m.get("status") == "honored":
        return {"ok": False, "error": "honored mementos live in the memorial"}

    try:
        import stress_store as _stress
        pressure_before = _stress.get_world_pressure()
        _remove_memento(memento_id)
        pressure_after = _stress.adjust_world_pressure(-2)
        return {
            "ok": True,
            "verb": "release",
            "memento_name": m.get("name", ""),
            "rarity": m.get("rarity", ""),
            "pressure_before": pressure_before,
            "pressure_after": pressure_after,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def sell_memento(memento_id: str) -> dict:
    """Sell a dormant/pending memento for XP. The memento is removed and
    the player gains XP scaled by rarity.

    Returns {ok, xp_gained, xp_total, memento_name}."""
    m = get_memento(memento_id)
    if not m:
        return {"ok": False, "error": "memento not found"}
    if m.get("kind") == "legacy":
        return {"ok": False, "error": "legacy relics cannot be sold"}
    if m.get("status") == "honored":
        return {"ok": False, "error": "honored mementos live in the memorial"}

    try:
        import stress_store as _stress
        rarity = (m.get("rarity") or "uncommon").lower()
        xp_amount = _stress.XP_PER_RARITY.get(rarity, 10)
        _remove_memento(memento_id)
        new_total = _stress.add_xp(xp_amount)
        return {
            "ok": True,
            "verb": "sell",
            "memento_name": m.get("name", ""),
            "rarity": rarity,
            "xp_gained": xp_amount,
            "xp_total": new_total,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Phase E: Equipment slots (4 per persona) ──────────────────────────
#
# Each persona can equip up to 4 mementos. Equipped mementos have their
# sidequests "hot" (eligible for scene matching via live_sidequests_for_scene).
# Unequipped mementos are "cold" (carried but sidequest won't fire).
# Stats from equipped mementos passively apply.
#
# Equipment is stored as a simple dict: {persona_id: [memento_id, ...]}
# in the main store file. The list is ordered (slot 0-3). Empty slots
# are None entries.
#
# Gated: the UI only shows equip slots when library size >= 5 (Phase H gate).
# The server enforces the 4-slot cap regardless of gate.

MAX_EQUIP_SLOTS = 4


def _load_equipment() -> dict:
    """Return the equipment map from the store. Backfills if missing."""
    with _lock:
        data = _load()
        data.setdefault("equipment", {})
        return data["equipment"]


def get_equipped(persona_id: str) -> list[Optional[str]]:
    """Return the list of equipped memento IDs for a persona (length 4,
    None entries for empty slots)."""
    eq = _load_equipment()
    slots = list(eq.get(persona_id, []))
    # Pad to MAX_EQUIP_SLOTS
    while len(slots) < MAX_EQUIP_SLOTS:
        slots.append(None)
    return slots[:MAX_EQUIP_SLOTS]


def equip_memento(persona_id: str, memento_id: str, slot: int = -1) -> dict:
    """Equip a memento into a persona's slot. If slot=-1, auto-fills the
    first empty slot. Returns {ok, slot, equipped_list}.

    Guards:
      - Memento must exist and be active (pending or dormant)
      - Persona must be an owner of the memento
      - Can't equip legacy relics (they're always-on)
      - Can't double-equip the same memento
    """
    m = get_memento(memento_id)
    if not m:
        return {"ok": False, "error": "memento not found"}
    if persona_id not in (m.get("owners") or []):
        return {"ok": False, "error": "persona is not an owner of this memento"}
    if m.get("kind") == "legacy":
        return {"ok": False, "error": "legacy relics are always active"}
    if m.get("status") == "honored":
        return {"ok": False, "error": "honored mementos live in the memorial"}

    with _lock:
        data = _load()
        eq = data.setdefault("equipment", {})
        slots = list(eq.get(persona_id, []))
        while len(slots) < MAX_EQUIP_SLOTS:
            slots.append(None)
        slots = slots[:MAX_EQUIP_SLOTS]

        # Guard: already equipped
        if memento_id in slots:
            return {"ok": False, "error": "already equipped"}

        # Pick slot
        target = slot
        if target < 0 or target >= MAX_EQUIP_SLOTS:
            # Auto-fill first empty
            target = -1
            for i, s in enumerate(slots):
                if s is None:
                    target = i
                    break
            if target < 0:
                return {"ok": False, "error": "all slots full — unequip something first"}

        # If target slot is occupied, swap out the old one
        slots[target] = memento_id
        eq[persona_id] = slots
        _save(data)

    return {"ok": True, "slot": target, "equipped": slots}


def unequip_memento(persona_id: str, memento_id: str) -> dict:
    """Remove a memento from a persona's equip slots. Returns {ok, equipped_list}."""
    with _lock:
        data = _load()
        eq = data.setdefault("equipment", {})
        slots = list(eq.get(persona_id, []))
        if memento_id not in slots:
            return {"ok": False, "error": "not equipped"}
        idx = slots.index(memento_id)
        slots[idx] = None
        eq[persona_id] = slots
        _save(data)
    return {"ok": True, "slot": idx, "equipped": slots}


def unequip_slot(persona_id: str, slot: int) -> dict:
    """Clear a specific slot index. Returns {ok, equipped_list}."""
    with _lock:
        data = _load()
        eq = data.setdefault("equipment", {})
        slots = list(eq.get(persona_id, []))
        while len(slots) < MAX_EQUIP_SLOTS:
            slots.append(None)
        if slot < 0 or slot >= MAX_EQUIP_SLOTS:
            return {"ok": False, "error": "invalid slot"}
        slots[slot] = None
        eq[persona_id] = slots
        _save(data)
    return {"ok": True, "slot": slot, "equipped": slots}


def is_equipped(persona_id: str, memento_id: str) -> bool:
    """Check if a memento is currently equipped by a specific persona."""
    return memento_id in get_equipped(persona_id)


def equipped_mementos_for(persona_id: str) -> list[dict]:
    """Return the full memento dicts for everything this persona has
    equipped, in slot order. Empty slots are skipped."""
    slots = get_equipped(persona_id)
    result = []
    for mid in slots:
        if mid:
            m = get_memento(mid)
            if m:
                result.append(m)
    return result


def equipped_stats_for(persona_id: str) -> dict:
    """Sum all stats from equipped mementos. Returns a stat dict like
    {str: 2, wis: 1, cha: -1}. Used by the session roll resolver to
    apply equipment bonuses."""
    totals = {}
    for m in equipped_mementos_for(persona_id):
        for k, v in (m.get("stats") or {}).items():
            totals[k] = totals.get(k, 0) + int(v)
    return totals


# ── Phase D.2: Honored memento passive drain ────────────────────────────
#
# Every honored (spent) memento in the memorial provides a passive stress
# drain bonus per session to BOTH owners. This is the primary counterplay
# to the stress spiral — the more sidequests you fulfill, the faster your
# party recovers. The memorial wall isn't just a museum; it's a healing
# engine that grows with every promise kept.

HONORED_DRAIN_BY_RARITY = {
    "common":    0.5,
    "uncommon":  0.75,
    "rare":      1.0,
    "epic":      1.5,
    "legendary": 2.0,
}


def compute_honored_drain() -> dict:
    """Compute the total passive stress drain each persona earns from
    honored mementos in the memorial. Returns {persona_id: total_drain}.

    Each honored memento contributes its rarity-based drain to BOTH owners.
    A persona who has honored 3 mementos (common + rare + uncommon) gets
    0.5 + 1.0 + 0.75 = 2.25 drain per session.
    """
    drains: dict[str, float] = {}
    with _lock:
        data = _load()
    for m in data.get("mementos", []):
        if m.get("status") != "honored":
            continue
        rarity = (m.get("transformed_from") or m.get("rarity") or "uncommon").lower()
        bonus = HONORED_DRAIN_BY_RARITY.get(rarity, 0.5)
        for owner in (m.get("owners") or []):
            drains[owner] = drains.get(owner, 0.0) + bonus
    return drains


def total_honored_drain_for(persona_id: str) -> float:
    """Return the total passive drain this persona earns from honored mementos."""
    return compute_honored_drain().get(persona_id, 0.0)


def _cli_show():
    data = _load()
    mementos = data["mementos"]
    print(f"Store: {STORE_FILE}")
    print(f"Mementos: {len(mementos)}")
    for m in sorted(mementos, key=lambda x: x.get("created_ts", 0), reverse=True):
        owners = " + ".join(m.get("owners", []))
        print(f"  [{m.get('rarity', '?'):10s}] {m.get('name', '?')}")
        print(f"    {owners}  ({m.get('kind', '?')})")
        desc = (m.get('desc') or '').strip()
        if desc:
            print(f"    -> {desc[:120]}")


if __name__ == "__main__":
    _cli_show()
