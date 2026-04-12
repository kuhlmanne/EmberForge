"""
session_store.py - Persistent store for active game sessions.

A "session" is a running mini-D&D scene between the player and their party,
narrated by The Keeper. Only one session can be active at a time in v1 (the
"current" session). When it ends, it's archived with a summary and memento
candidates; the next session starts fresh.

Schema of .sessions.json:
{
  "active": {
    "id":            "<uuid>",
    "created_ts":    1713...,
    "updated_ts":    1713...,
    "player_name":   "Eric",
    "party":         ["shadowheart", "tesla", ...],
    "scenario_hint": "(optional player hint)",
    "messages": [
      {"role": "keeper",  "text": "...",           "ts": ...},
      {"role": "player",  "text": "...",           "ts": ...},
      {"role": "keeper",  "text": "...",           "ts": ...}
    ],
    "status":        "active" | "ending" | "ended"
  },
  "archive": [
    { "id": "...", "ended_ts": ..., "summary": "...", "mementos_pending": [...], ... }
  ]
}

Only one session is active at a time. The archive is pruned to the most
recent N sessions to keep file size sane.

API (thread-safe):
  start_session(player_name, party, scenario_hint) -> session_dict
  get_active() -> session_dict | None
  append_message(role, text) -> None
  set_status(status) -> None
  end_session(summary, mementos_pending) -> archived session_dict
  recent_archive(n=10) -> list[dict]
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

WINGMAN_DIR = Path(__file__).parent
STORE_FILE  = WINGMAN_DIR / ".sessions.json"
ARCHIVE_MAX = 50

_lock = threading.RLock()


def _load() -> dict:
    if not STORE_FILE.exists():
        return {"active": None, "archive": []}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("active", None)
        data.setdefault("archive", [])
        return data
    except Exception:
        return {"active": None, "archive": []}


def _save(data: dict) -> None:
    tmp = STORE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(STORE_FILE)


def start_session(
    player_name: str,
    party: list[str],
    scenario_hint: str = "",
) -> dict:
    """Create a new active session. Overwrites any existing active session
    (v1 constraint: one active session at a time).

    Turn-based CRPG session schema:
      - turn_order : list of "player" | persona_id, set after opening
      - turn_idx   : int, current position in turn_order
      - beats      : list of per-turn reaction beats,
                     each { who: "keeper"|"player"|persona_id,
                            text: str, ts: float }
      - messages is kept as a flat log of all exchanges (Keeper + player)
                 for backward compat with code that reads it. Beats is
                 the authoritative turn-based log."""
    session = {
        "id":              uuid.uuid4().hex[:12],
        "created_ts":      time.time(),
        "updated_ts":      time.time(),
        "player_name":     player_name or "you",
        "party":           list(party),
        "scenario_hint":   scenario_hint or "",
        "messages":        [],
        "beats":           [],        # authoritative turn log
        "turn_order":      [],        # set by Keeper after opening
        "turn_idx":        0,         # index into turn_order
        "turn_deadline":   0,         # unix ts when player turn expires
        "party_suggestions": [],      # voiced in planning phase: [{pid, text}]
        "encounter_dc":    14,        # difficulty set by Keeper in opening
        "encounter_type":  "social",  # type set by Keeper (determines stat)
        "encounter_rolls": [],        # per-turn roll log: [{who, roll, total, margin, outcome, stress_delta}]
        "phase":           "planning",# planning | active | resolving | ended
        "status":          "active",  # legacy top-level status; kept for UI
    }
    with _lock:
        data = _load()
        # If there's a stale active session, archive it as abandoned
        if data.get("active") and data["active"].get("status") == "active":
            old = data["active"]
            old["status"] = "abandoned"
            old["ended_ts"] = time.time()
            old["summary"] = "Session abandoned when a new one was started."
            old["mementos_pending"] = []
            data["archive"].insert(0, old)
            data["archive"] = data["archive"][:ARCHIVE_MAX]
        data["active"] = session
        _save(data)
    return session


def get_active() -> Optional[dict]:
    with _lock:
        data = _load()
        return data.get("active")


def append_message(role: str, text: str, who: str = "") -> None:
    """Append a message to the active session. Role is typically 'keeper'
    or 'player'. 'who' is optional for party-member dialogue attributed to
    a specific persona."""
    entry = {"role": role, "text": text, "ts": time.time()}
    if who:
        entry["who"] = who
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"].setdefault("messages", []).append(entry)
        data["active"]["updated_ts"] = time.time()
        _save(data)


def set_status(status: str) -> None:
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"]["status"] = status
        data["active"]["updated_ts"] = time.time()
        _save(data)


def set_phase(phase: str) -> None:
    """Set the session phase: planning | active | resolving | ended."""
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"]["phase"] = phase
        data["active"]["updated_ts"] = time.time()
        _save(data)


def get_phase() -> Optional[str]:
    with _lock:
        data = _load()
        return (data.get("active") or {}).get("phase")


def set_encounter_dc(dc: int) -> None:
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"]["encounter_dc"] = int(dc)
        _save(data)


def set_encounter_type(t: str) -> None:
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"]["encounter_type"] = str(t).lower()
        _save(data)


def add_encounter_roll(entry: dict) -> None:
    """Append a hidden roll entry to the session's encounter_rolls log."""
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"].setdefault("encounter_rolls", []).append(entry)
        data["active"]["updated_ts"] = time.time()
        _save(data)


def set_party_suggestions(suggestions: list[dict]) -> None:
    """Store the party's initial suggestions voiced during the planning
    phase. Each entry is {pid: ..., text: ...}."""
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"]["party_suggestions"] = list(suggestions)
        data["active"]["updated_ts"] = time.time()
        _save(data)


# ── Turn-based session helpers ──────────────────────────────────────────

def set_turn_order(order: list[str]) -> None:
    """Set the turn order for the current session. Entries are 'player' or
    persona_ids. Resets turn_idx to 0."""
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"]["turn_order"] = list(order)
        data["active"]["turn_idx"] = 0
        data["active"]["updated_ts"] = time.time()
        _save(data)


def get_turn_order() -> list[str]:
    with _lock:
        data = _load()
        return list((data.get("active") or {}).get("turn_order", []))


def current_turn() -> Optional[str]:
    """Return the entry at the current turn_idx, or None if out of range."""
    with _lock:
        data = _load()
        session = data.get("active")
        if not session:
            return None
        order = session.get("turn_order", [])
        idx   = int(session.get("turn_idx", 0))
        if idx < 0 or idx >= len(order):
            return None
        return order[idx]


def advance_turn_idx() -> int:
    """Advance turn_idx by 1. Returns the new index."""
    with _lock:
        data = _load()
        session = data.get("active")
        if not session:
            return 0
        session["turn_idx"] = int(session.get("turn_idx", 0)) + 1
        session["updated_ts"] = time.time()
        _save(data)
        return session["turn_idx"]


def add_beat(who: str, text: str) -> None:
    """Append a per-turn beat to the session. 'who' is a persona_id,
    'player', or 'keeper'. Also mirrors into the flat messages log for
    backward compat."""
    entry = {"who": who, "text": text, "ts": time.time()}
    msg_role = "keeper" if who == "keeper" else ("player" if who == "player" else "keeper")
    # A party member's voiced reaction counts as keeper output in messages
    # (the Keeper is the one writing their dialogue) - preserves session
    # replay fidelity for session_forge._build_messages_from_session.
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"].setdefault("beats", []).append(entry)
        data["active"].setdefault("messages", []).append({
            "role": msg_role,
            "who":  who,
            "text": text,
            "ts":   entry["ts"],
        })
        data["active"]["updated_ts"] = time.time()
        _save(data)


def set_turn_deadline(deadline_ts: float) -> None:
    """Set the wall-clock deadline for the current turn (used for player
    turns with a 30-second timer)."""
    with _lock:
        data = _load()
        if not data.get("active"):
            return
        data["active"]["turn_deadline"] = float(deadline_ts)
        _save(data)


def interrupt_to_player() -> bool:
    """Move 'player' to be the next turn in the current order. If the
    player is already the current turn, this is a no-op. Returns True
    if the queue was modified."""
    with _lock:
        data = _load()
        session = data.get("active")
        if not session:
            return False
        order = list(session.get("turn_order", []))
        idx   = int(session.get("turn_idx", 0))
        if idx < 0 or idx > len(order):
            return False
        # If player is already current, nothing to do
        if idx < len(order) and order[idx] == "player":
            return False
        # Find the next player occurrence after idx
        try:
            player_pos = order.index("player", idx)
        except ValueError:
            # Player wasn't in the remaining order - insert them at idx
            order.insert(idx, "player")
            session["turn_order"] = order
            _save(data)
            return True
        # Move player from player_pos to idx
        if player_pos == idx:
            return False
        order.pop(player_pos)
        order.insert(idx, "player")
        session["turn_order"] = order
        _save(data)
        return True


def end_session(
    summary: str = "",
    mementos_pending: Optional[list] = None,
    xp_earned: int = 0,
) -> Optional[dict]:
    """Archive the active session and clear it. Returns the archived entry."""
    with _lock:
        data = _load()
        active = data.get("active")
        if not active:
            return None
        active["status"] = "ended"
        active["ended_ts"] = time.time()
        active["summary"] = summary or ""
        active["mementos_pending"] = mementos_pending or []
        active["xp_earned"] = int(xp_earned or 0)
        data["archive"].insert(0, active)
        data["archive"] = data["archive"][:ARCHIVE_MAX]
        data["active"] = None
        _save(data)
        return active


def recent_archive(n: int = 10) -> list[dict]:
    with _lock:
        data = _load()
        return list(data.get("archive", [])[:n])


def _cli_show():
    data = _load()
    active = data.get("active")
    print(f"Store: {STORE_FILE}")
    if active:
        print(f"ACTIVE session {active['id']}")
        print(f"  player={active['player_name']}  party={active['party']}")
        print(f"  messages={len(active.get('messages', []))}  status={active['status']}")
    else:
        print("No active session.")
    archive = data.get("archive", [])
    print(f"Archived: {len(archive)}")
    for s in archive[:5]:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get('ended_ts', s.get('created_ts', 0))))
        print(f"  [{s['id']}] {ts}  {s.get('status', '?')}  {s.get('summary', '')[:60]}")


if __name__ == "__main__":
    _cli_show()
