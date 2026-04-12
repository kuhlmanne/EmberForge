"""
persona_registry.py — Single source of truth for Amplify's persona framework.

Loads all persona definitions from personas/*.json and exposes a query API
used by room_agent.py, idle_engine.py, the tray panel, and the studio UI.

Persona lifecycle states (the "BG3 camp" metaphor):
    catalog — defined but not built (may lack voice/prompt/image)
    built   — has all required fields; ready to be dropped into camp
    camp    — cached in the party's shared space; can be dropped into party
    party   — active companion, can hear Eric, respond, and banter

State transitions happen via drop_in() / drop_out() which persist to the
JSON files. Hot-reload is supported: call load() again to pick up manual edits.

API surface:
    load()                               — (re)load all personas from disk
    all_personas() -> list[Persona]      — every persona known to the registry
    camp()        -> list[Persona]       — status in {camp, party}
    party()       -> list[Persona]       — status == party
    get(id)       -> Persona | None
    wake_aliases_for_party() -> list[tuple[str, str]]
    detect_addressee(transcript) -> str | None
    drop_in(id)   — camp -> party
    drop_out(id)  — party -> camp
    is_enabled(id) -> bool               — party-member AND ambient_mode != "off"
    build_party_context(active_id) -> str  — injected into LLM system prompt
    build_system_prompt(active_id, memory_block="") -> str  — full prompt for active

Schema version: 1 (see personas/*.json _schema_version field)
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("persona_registry")

WINGMAN_DIR   = Path(__file__).parent
PERSONAS_DIR  = WINGMAN_DIR / "personas"

# Files in personas/ that are NOT persona definitions (config, voice source lists, etc.)
_NON_PERSONA_FILENAMES = {
    "edge_voice_sources.json",
    "el_voice_sources.json",
}


@dataclass
class Persona:
    """In-memory representation of a single persona JSON file."""
    id: str
    display_name: str
    origin: str
    era: str
    status: str               # catalog | built | camp | party
    archetype: str
    essence: str
    profile_image: str
    portrait_art_prompt: str
    voice: dict               # engine, sample, sample_source, style_hint, cadence_notes
    wake_aliases: list[str]
    persona_prompt: str
    banter_voice_label: str
    relationships: dict[str, str]
    quirks: list[str]
    signature_quotes: list[str]
    knowledge_cutoff: str
    knowledge_domains: list[str]
    ignorance_domains: list[str]
    reference_urls: list[str] = field(default_factory=list)
    research_notes: str = ""
    _raw: dict = field(default_factory=dict, repr=False)  # full JSON for pass-through
    _source_path: Optional[Path] = None

    @classmethod
    def from_json(cls, data: dict, source_path: Optional[Path] = None) -> "Persona":
        return cls(
            id=data["id"],
            display_name=data.get("display_name", data["id"]),
            origin=data.get("origin", "?"),
            era=data.get("era", "?"),
            status=data.get("status", "catalog"),
            archetype=data.get("archetype", "?"),
            essence=data.get("essence", ""),
            profile_image=data.get("profile_image", ""),
            portrait_art_prompt=data.get("portrait_art_prompt", ""),
            voice=data.get("voice", {}),
            wake_aliases=list(data.get("wake_aliases", [])),
            persona_prompt=data.get("persona_prompt", ""),
            banter_voice_label=data.get("banter_voice_label", ""),
            relationships=dict(data.get("relationships", {})),
            quirks=list(data.get("quirks", [])),
            signature_quotes=list(data.get("signature_quotes", [])),
            knowledge_cutoff=data.get("knowledge_cutoff", ""),
            knowledge_domains=list(data.get("knowledge_domains", [])),
            ignorance_domains=list(data.get("ignorance_domains", [])),
            reference_urls=list(data.get("reference_urls", [])),
            research_notes=data.get("research_notes", ""),
            _raw=data,
            _source_path=source_path,
        )


class _Registry:
    """Internal singleton registry. Use module-level functions below."""

    def __init__(self) -> None:
        self._personas: dict[str, Persona] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        """(Re)load all persona JSON files from personas/ directory."""
        with self._lock:
            self._personas.clear()
            if not PERSONAS_DIR.exists():
                log.warning(f"personas/ directory not found at {PERSONAS_DIR}")
                return
            loaded = 0
            for json_file in sorted(PERSONAS_DIR.glob("*.json")):
                if json_file.name in _NON_PERSONA_FILENAMES:
                    continue
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    persona = Persona.from_json(data, source_path=json_file)
                    self._personas[persona.id] = persona
                    loaded += 1
                except Exception as e:
                    log.error(f"Failed to load {json_file.name}: {e}")
            log.info(f"Loaded {loaded} personas: {sorted(self._personas.keys())}")

    def save(self, persona_id: str) -> None:
        """Persist a single persona back to its source JSON file."""
        with self._lock:
            p = self._personas.get(persona_id)
            if not p or not p._source_path:
                return
            data = dict(p._raw)
            data["status"] = p.status  # reflect any state changes
            with open(p._source_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            log.info(f"Saved {persona_id} -> {p._source_path.name}")

    def all_personas(self) -> list[Persona]:
        with self._lock:
            return list(self._personas.values())

    def camp(self) -> list[Persona]:
        with self._lock:
            return [p for p in self._personas.values() if p.status in ("camp", "party")]

    def party(self) -> list[Persona]:
        with self._lock:
            return [p for p in self._personas.values() if p.status == "party"]

    def get(self, persona_id: str) -> Optional[Persona]:
        with self._lock:
            return self._personas.get(persona_id)

    def wake_aliases_for_party(self) -> list[tuple[str, str]]:
        """Flat list of (alias, persona_id) for every in-party persona.
        Order matters: longer/more specific aliases first so substring matching
        doesn't route 'shadowheart' to a persona whose alias is 'shadow'."""
        with self._lock:
            pairs: list[tuple[str, str]] = []
            for p in self.party():
                for alias in p.wake_aliases:
                    pairs.append((alias.lower(), p.id))
            # Sort by alias length descending so specific names win
            pairs.sort(key=lambda x: -len(x[0]))
            return pairs

    def detect_addressee(self, transcript: str) -> Optional[str]:
        """Return the persona id of whoever Eric is addressing, or None.

        Replaces room_agent._detect_character(). Walks all in-party personas'
        wake aliases and returns the first match. Longer aliases win (so
        'shadowheart' matches before 'shadow').
        """
        t_lower = transcript.lower()
        for alias, pid in self.wake_aliases_for_party():
            if alias in t_lower:
                log.info(f"[detect_addressee] matched alias '{alias}' -> {pid}")
                return pid
        return None

    def drop_in(self, persona_id: str) -> bool:
        """Move a persona from camp to party. Returns True on success."""
        with self._lock:
            p = self._personas.get(persona_id)
            if not p:
                return False
            if p.status == "party":
                return True  # already there
            if p.status not in ("camp", "built"):
                log.warning(f"Cannot drop {persona_id} into party from status={p.status}")
                return False
            p.status = "party"
            self.save(persona_id)
            log.info(f"[drop_in] {persona_id} -> party")
            return True

    def drop_out(self, persona_id: str) -> bool:
        """Move a persona from party to camp. Returns True on success."""
        with self._lock:
            p = self._personas.get(persona_id)
            if not p:
                return False
            if p.status != "party":
                return True  # not in party, nothing to do
            p.status = "camp"
            self.save(persona_id)
            log.info(f"[drop_out] {persona_id} -> camp")
            return True

    def is_enabled(self, persona_id: str, ambient_mode: Optional[str] = None) -> bool:
        """Return True if the persona should currently respond.

        Conditions:
        - Persona is in party
        - ambient_mode is not "off" (master kill switch) — if ambient_mode is
          None, it's not checked (caller can pass it to enforce).
        """
        with self._lock:
            p = self._personas.get(persona_id)
            if not p:
                return False
            if p.status != "party":
                return False
            if ambient_mode == "off":
                return False
            return True

    def build_party_context(self, active_id: str) -> str:
        """Build the 'you are in a party with...' context block for LLM injection.

        Returned text is appended to the active persona's system prompt at
        call time. Includes the active persona's relationship notes about
        each other in-party member so they can reference each other naturally.
        """
        with self._lock:
            active = self._personas.get(active_id)
            if not active:
                return ""
            others = [p for p in self.party() if p.id != active_id]
            if not others:
                return "\n\nPARTY CONTEXT: You are alone with Eric. No other companions are present."
            lines = ["\n\nPARTY CONTEXT: You are currently in a party with the following companions. They may listen in; reference them naturally when relevant."]
            for other in others:
                rel_note = active.relationships.get(other.id, "")
                lines.append(f"\n- {other.display_name} ({other.origin}, {other.archetype}).")
                if rel_note:
                    lines.append(f"  Your relationship: {rel_note}")
            return "\n".join(lines)

    def build_system_prompt(self, active_id: str, memory_block: str = "") -> Optional[str]:
        """Compose the full system prompt for the active persona.

        Takes the persona's persona_prompt, substitutes {memory_block},
        and appends the party context block. Returns None if the persona
        doesn't exist.
        """
        with self._lock:
            p = self._personas.get(active_id)
            if not p:
                return None
            prompt = p.persona_prompt.replace("{memory_block}", memory_block)
            prompt += self.build_party_context(active_id)
            return prompt


# ── Module-level singleton + public API ─────────────────────────────────────

_registry: Optional[_Registry] = None
_init_lock = threading.Lock()


def _ensure() -> _Registry:
    global _registry
    if _registry is None:
        with _init_lock:
            if _registry is None:
                _registry = _Registry()
    return _registry


def load() -> None:
    _ensure().load()


def all_personas() -> list[Persona]:
    return _ensure().all_personas()


def camp() -> list[Persona]:
    return _ensure().camp()


def party() -> list[Persona]:
    return _ensure().party()


def get(persona_id: str) -> Optional[Persona]:
    return _ensure().get(persona_id)


def wake_aliases_for_party() -> list[tuple[str, str]]:
    return _ensure().wake_aliases_for_party()


def detect_addressee(transcript: str) -> Optional[str]:
    return _ensure().detect_addressee(transcript)


def drop_in(persona_id: str) -> bool:
    return _ensure().drop_in(persona_id)


def drop_out(persona_id: str) -> bool:
    return _ensure().drop_out(persona_id)


def is_enabled(persona_id: str, ambient_mode: Optional[str] = None) -> bool:
    return _ensure().is_enabled(persona_id, ambient_mode=ambient_mode)


def build_party_context(active_id: str) -> str:
    return _ensure().build_party_context(active_id)


def build_system_prompt(active_id: str, memory_block: str = "") -> Optional[str]:
    return _ensure().build_system_prompt(active_id, memory_block=memory_block)


# ── CLI smoke test ──────────────────────────────────────────────────────────

def _main() -> None:
    """python persona_registry.py  — prints a summary of all loaded personas."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    reg = _ensure()
    print(f"Loaded {len(reg.all_personas())} personas from {PERSONAS_DIR}")
    print()
    print(f"{'ID':<15} {'STATUS':<8} {'ALIASES':<8} {'DISPLAY NAME'}")
    print("-" * 70)
    for p in reg.all_personas():
        print(f"{p.id:<15} {p.status:<8} {len(p.wake_aliases):<8} {p.display_name}")
    print()
    print(f"CAMP  ({len(reg.camp())}):  {[p.id for p in reg.camp()]}")
    print(f"PARTY ({len(reg.party())}): {[p.id for p in reg.party()]}")
    print()
    # Smoke test detect_addressee
    test_transcripts = [
        "Shadowheart, what time is it?",
        "Hey Tesla, can you explain resonance?",
        "Lae'zel, handle this task.",
        "Jefferson, what would you have done?",
        "Kerrigan, reports?",
        "Eric is making coffee.",  # should return None — no one addressed
    ]
    print("detect_addressee() smoke test:")
    for t in test_transcripts:
        pid = reg.detect_addressee(t)
        print(f"  {t!r:<50} -> {pid}")


if __name__ == "__main__":
    _main()
