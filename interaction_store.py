"""
interaction_store.py — Persistent cross-persona interaction history.

Stores two kinds of data, both survive across process restarts and persona
status transitions (catalog/camp/party):

1. PAIR interactions: for every (a, b) pair of personas that have ever
   exchanged dialogue — count, first/last timestamps, an accumulating
   arc summary. Used to make future exchanges feel like continuations.

2. PERSONA presence: for every persona — first_seen timestamp, plus
   rolling lists of party_enters/party_exits. Used to answer "how long
   have they been gone" and "is this their first time in the room".

Storage: a single .persona_interactions.json file at repo root. Writes
are serialized via a module-level lock. Reads grab the lock briefly and
return copies.

API (thread-safe):
    record_pair_exchange(a, b, summary=None) -> pair_dict
    record_persona_event(id, kind)           -> None  kind in {party_enter, party_exit, camp_enter, camp_exit}
    get_pair(a, b)                           -> pair_dict | None
    get_persona(id)                          -> persona_dict | None
    format_relative_time(seconds)            -> str
    forget_persona(id)                       -> None  (wipe all memory touching persona)
    all_pairs_for(id)                        -> dict  {other_id: pair_dict}
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

WINGMAN_DIR = Path(__file__).parent
STORE_FILE  = WINGMAN_DIR / ".persona_interactions.json"

_lock = threading.RLock()


def _pair_key(a: str, b: str) -> str:
    """Canonical sorted key for a pair so (a,b) and (b,a) hit the same entry."""
    lo, hi = sorted([a, b])
    return f"{lo}+{hi}"


def _load() -> dict:
    if not STORE_FILE.exists():
        return {"pairs": {}, "personas": {}}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Defensive: ensure both sections exist
        data.setdefault("pairs", {})
        data.setdefault("personas", {})
        return data
    except Exception:
        return {"pairs": {}, "personas": {}}


def _save(data: dict) -> None:
    # Write to tmp then atomic rename to avoid corruption on concurrent reads
    tmp = STORE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(STORE_FILE)


# ── Pair API ───────────────────────────────────────────────────────────────

def record_pair_exchange(a: str, b: str, summary: Optional[str] = None) -> dict:
    """Increment a pair's exchange count and update last_ts / last_summary.

    If summary is None, just bumps count and last_ts (useful for recording
    that an exchange happened before the arc summary is ready).
    Returns the updated pair entry.
    """
    with _lock:
        data = _load()
        key = _pair_key(a, b)
        pair = data["pairs"].get(key, {
            "a": sorted([a, b])[0],
            "b": sorted([a, b])[1],
            "count": 0,
            "first_ts": None,
            "last_ts": None,
            "last_summary": None,
            "arc_hint": None,
        })
        now = time.time()
        pair["count"] = int(pair.get("count", 0)) + 1
        if pair.get("first_ts") is None:
            pair["first_ts"] = now
        pair["last_ts"] = now
        if summary:
            pair["last_summary"] = summary
        data["pairs"][key] = pair
        _save(data)
        return dict(pair)


def update_pair_summary(a: str, b: str, summary: str, arc_hint: Optional[str] = None) -> None:
    """Update just the summary/arc_hint without bumping count.
    Used by the background summary thread after an exchange completes.
    """
    with _lock:
        data = _load()
        key = _pair_key(a, b)
        if key not in data["pairs"]:
            # Haven't recorded the exchange yet — create a placeholder
            data["pairs"][key] = {
                "a": sorted([a, b])[0],
                "b": sorted([a, b])[1],
                "count": 1,
                "first_ts": time.time(),
                "last_ts": time.time(),
                "last_summary": None,
                "arc_hint": None,
            }
        data["pairs"][key]["last_summary"] = summary
        if arc_hint is not None:
            data["pairs"][key]["arc_hint"] = arc_hint
        _save(data)


def get_pair(a: str, b: str) -> Optional[dict]:
    with _lock:
        data = _load()
        return data["pairs"].get(_pair_key(a, b))


def all_pairs_for(persona_id: str) -> dict[str, dict]:
    """Return {other_id: pair_dict} for every pair involving persona_id."""
    with _lock:
        data = _load()
        result: dict[str, dict] = {}
        for key, pair in data["pairs"].items():
            if pair.get("a") == persona_id:
                result[pair["b"]] = dict(pair)
            elif pair.get("b") == persona_id:
                result[pair["a"]] = dict(pair)
        return result


# ── Persona presence API ───────────────────────────────────────────────────

_PERSONA_EVENT_KINDS = {
    "party_enter": "party_enters",
    "party_exit":  "party_exits",
    "camp_enter":  "camp_enters",
    "camp_exit":   "camp_exits",
}

# Cap rolling event lists so the file doesn't grow unbounded
_MAX_EVENTS = 50


def record_persona_event(persona_id: str, kind: str) -> None:
    """Append a timestamped event to a persona's presence history."""
    if kind not in _PERSONA_EVENT_KINDS:
        return
    with _lock:
        data = _load()
        personas = data["personas"]
        entry = personas.get(persona_id, {
            "first_seen": None,
            "party_enters": [],
            "party_exits":  [],
            "camp_enters":  [],
            "camp_exits":   [],
        })
        now = time.time()
        if entry.get("first_seen") is None:
            entry["first_seen"] = now
        field = _PERSONA_EVENT_KINDS[kind]
        lst = entry.setdefault(field, [])
        lst.append(now)
        if len(lst) > _MAX_EVENTS:
            # Keep the most recent N events
            entry[field] = lst[-_MAX_EVENTS:]
        personas[persona_id] = entry
        _save(data)


def get_persona(persona_id: str) -> Optional[dict]:
    with _lock:
        data = _load()
        return data["personas"].get(persona_id)


def seconds_since_last_party_exit(persona_id: str) -> Optional[float]:
    """Returns seconds since the last time this persona LEFT the party.
    None if they have never been in the party or never left.
    """
    entry = get_persona(persona_id)
    if not entry:
        return None
    exits = entry.get("party_exits", [])
    if not exits:
        return None
    return time.time() - exits[-1]


def seconds_since_first_seen(persona_id: str) -> Optional[float]:
    entry = get_persona(persona_id)
    if not entry or entry.get("first_seen") is None:
        return None
    return time.time() - entry["first_seen"]


def forget_persona(persona_id: str) -> None:
    """Wipe ALL interaction history touching this persona.
    Use for hard resets — treats the persona as never-met on next encounter.
    """
    with _lock:
        data = _load()
        # Drop pair entries touching this persona
        keys_to_drop = [k for k, p in data["pairs"].items()
                         if p.get("a") == persona_id or p.get("b") == persona_id]
        for k in keys_to_drop:
            del data["pairs"][k]
        # Drop persona entry
        if persona_id in data["personas"]:
            del data["personas"][persona_id]
        _save(data)


# ── Relative time formatting ───────────────────────────────────────────────

def format_relative_time(seconds: float) -> str:
    """Human-readable relative time for prompt injection.

    Ranges chosen for narrative flavor, not precision:
        just now         < 10 min
        an hour ago      < 90 min
        earlier today    < 6 hours
        this morning     < 24 hours (if wall-clock morning-ish)
        yesterday        < 48 hours
        N days ago       < 7 days
        last week        < 14 days
        weeks ago        < 60 days
        a while ago      otherwise
    """
    if seconds < 0:
        return "just now"
    mins = seconds / 60
    hours = seconds / 3600
    days = seconds / 86400
    if mins < 10:
        return "just now"
    if mins < 90:
        return "an hour ago"
    if hours < 6:
        return "earlier today"
    if hours < 24:
        return "earlier"
    if hours < 48:
        return "yesterday"
    if days < 7:
        return f"{int(days)} days ago"
    if days < 14:
        return "last week"
    if days < 60:
        return f"{int(days // 7)} weeks ago"
    return "a while ago"


# ── CLI inspection ─────────────────────────────────────────────────────────

def _cli_show():
    """python interaction_store.py — print a summary of all stored state."""
    with _lock:
        data = _load()
    print(f"Store: {STORE_FILE}")
    print(f"Pairs: {len(data['pairs'])}")
    for key, pair in sorted(data["pairs"].items(), key=lambda kv: -(kv[1].get("last_ts") or 0)):
        since = ""
        if pair.get("last_ts"):
            since = format_relative_time(time.time() - pair["last_ts"])
        print(f"  {key:30s} count={pair.get('count', 0):3d}  last={since}")
        if pair.get("last_summary"):
            print(f"    -> {pair['last_summary'][:100]}")
    print()
    print(f"Personas: {len(data['personas'])}")
    for pid, entry in sorted(data["personas"].items()):
        seen = entry.get("first_seen")
        first = format_relative_time(time.time() - seen) if seen else "never"
        pe = len(entry.get("party_enters", []))
        px = len(entry.get("party_exits", []))
        print(f"  {pid:15s} first_seen={first:15s}  party_enters={pe} party_exits={px}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "forget" and len(sys.argv) == 3:
        forget_persona(sys.argv[2])
        print(f"Forgot: {sys.argv[2]}")
    else:
        _cli_show()
