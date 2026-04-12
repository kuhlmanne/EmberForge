"""
persona_affinities.py - Strategic affinities for turn-order composition.

Each persona has:
  - affinity_tags: short labels for what they shape (analytical, stealth, etc.)
  - when_first:    what the scene leans toward if they react first in a round
  - when_last:     what the scene leans toward if they close the round
  - adjacency:     (future) how they support or weaken neighboring turns

The Keeper reads this in her session briefing when she proposes a turn order.
The player reads this (implicitly, via her narration + the party suggestions
in the planning phase) when they decide whether to override her order.

Changing the order literally reshapes the scene - putting Laezel first routes
toward force, putting Shadowheart first routes toward stealth, Tesla first
frontloads analysis so later reactions benefit from his read.

This is hand-authored for our core 5. The persona forge will generate these
for newly-summoned personas by reading their essence + inner weight.
"""

from __future__ import annotations
from typing import Optional

# Unicode glyphs used as affinity icons in the turn strip. Kept small and
# monochrome so they read at 12-14px.
AFFINITY_ICONS: dict[str, str] = {
    "stealth":     "\u263e",   # crescent moon
    "empathic":    "\u2665",   # heart
    "cautious":    "\u22a0",   # boxed cross / shield
    "observant":   "\u25c9",   # fisheye
    "combat":      "\u2694",   # crossed swords
    "direct":      "\u27a4",   # arrowhead
    "dominance":   "\u2736",   # 6-pointed star
    "decisive":    "\u2726",   # 4-pointed star
    "analytical":  "\u2609",   # sun (sol)
    "technical":   "\u2699",   # gear
    "perceptive":  "\u2735",   # 8-pointed star
    "curious":     "\u25cb",   # ring
    "diplomatic":  "\u2625",   # ankh
    "rhetorical":  "\u2767",   # fleur ornament
    "strategic":   "\u269c",   # fleur-de-lis
    "psionic":     "\u272a",   # star circled
    "commanding":  "\u2721",   # star of david
    "survivor":    "\u2623",   # biohazard
    "balanced":    "\u269d",   # outlined white star
}

# Which word describes the strategic posture when a given affinity is in
# slot 1 (used for the "Approach feels: X" readout above the turn strip).
APPROACH_WORDS: dict[str, str] = {
    "stealth":    "quiet",
    "empathic":   "held",
    "cautious":   "careful",
    "combat":     "direct",
    "direct":     "direct",
    "dominance":  "dominant",
    "decisive":   "decisive",
    "analytical": "analytical",
    "technical":  "technical",
    "perceptive": "watchful",
    "curious":    "probing",
    "diplomatic": "spoken",
    "rhetorical": "spoken",
    "strategic":  "measured",
    "psionic":    "sovereign",
    "commanding": "sovereign",
    "survivor":   "hardened",
    "balanced":   "level",
}

PERSONA_AFFINITIES: dict[str, dict] = {
    "shadowheart": {
        "affinity_tags": ["stealth", "empathic", "cautious", "observant"],
        "when_first_tag": "The Silence",
        "when_last_tag":  "The Private Witness",
        "when_first": (
            "If Shadowheart reacts first, the party tilts toward SILENCE. "
            "Violence becomes a last resort. Stealth and patience dominate. "
            "The scene will favor careful reads over bold moves, and negotiations "
            "opened later will start from a position of gathered knowledge. "
            "Cost: the party misses fast-closing opportunities."
        ),
        "when_last": (
            "If Shadowheart closes the round, she offers the private witness. "
            "What the scene cost her is named. Mementos forged from this round "
            "gain rarity because the grief was seen. The resolution is quieter "
            "and more personal - the scene leaves marks rather than trophies."
        ),
    },
    "laezel": {
        "affinity_tags": ["combat", "direct", "dominance", "decisive"],
        "when_first_tag": "The Edge",
        "when_last_tag":  "The Decisive Strike",
        "when_first": (
            "If Lae'zel reacts first, the party tilts toward FORCE. The scene "
            "locks into a posture of readiness. Diplomatic resolutions become "
            "harder to reach but the path through combat is clearer and "
            "cleaner. Threats are named immediately. Cost: the party burns any "
            "chance of a quiet solution."
        ),
        "when_last": (
            "If Lae'zel closes the round, she delivers the decisive strike - "
            "resolution by action, not reflection. Virtue moments are more "
            "likely (Stalwart / Courageous) but rescue windows shrink. The "
            "scene leaves a body or a bond, rarely a question."
        ),
    },
    "tesla": {
        "affinity_tags": ["analytical", "technical", "perceptive", "curious"],
        "when_first_tag": "The Read",
        "when_last_tag":  "Retrospective Clarity",
        "when_first": (
            "If Tesla reacts first, the party FRONTLOADS ANALYSIS. The "
            "situation is read before it is acted on. Every reaction after "
            "his is better-informed, and whichever party member closes the "
            "round gains clarity from what he spotted. Cost: the scene moves "
            "slower at the top, and in fast-developing threats, slow is bad."
        ),
        "when_last": (
            "If Tesla closes the round, the analysis becomes RETROSPECTIVE. "
            "He names what the encounter actually meant, and the party gains "
            "an XP modifier from the reflection - they learned something about "
            "what they were doing. Best suited to scenes with a puzzle at their "
            "center."
        ),
    },
    "jefferson": {
        "affinity_tags": ["diplomatic", "rhetorical", "analytical", "strategic"],
        "when_first_tag": "Opening Words",
        "when_last_tag":  "The Elegy",
        "when_first": (
            "If Jefferson reacts first, the party FRAMES THE SCENE IN WORDS. "
            "Social and negotiated outcomes become more available. NPCs are "
            "more likely to respond to address than to sword. Cost: if the "
            "situation was never going to yield to words, he delays the "
            "party's access to the only resolution that actually worked."
        ),
        "when_last": (
            "If Jefferson closes the round, he delivers the ELEGY. He names "
            "what was weighed, what was chosen, what was left unchosen, and "
            "the moral ledger of the scene. Party BONDS strengthen from this "
            "retrospective accounting. Relationships between party members "
            "deepen whether the scene succeeded or failed."
        ),
    },
    "kerrigan": {
        "affinity_tags": ["psionic", "commanding", "decisive", "survivor"],
        "when_first_tag": "The Power Posture",
        "when_last_tag":  "The Reckoning",
        "when_first": (
            "If Kerrigan reacts first, the party takes a POWER POSTURE. Weak "
            "NPCs fold immediately; strong ones escalate. The scene binarizes: "
            "fewer compromises, more definitive outcomes in either direction. "
            "Cost: middle-ground solutions vanish from the option space."
        ),
        "when_last": (
            "If Kerrigan closes the round, she delivers the RECKONING. The "
            "scene is read from the perspective of someone who has broken "
            "before and remembers what it cost. Edges forged from this round "
            "tend to be practical (single-use advantages for the next scene) "
            "rather than sentimental. She gives the party survival lessons, "
            "not comfort."
        ),
    },
}

# Fallback affinities for personas without a hand-authored entry.
# Used by forge-generated personas until their specific affinities are
# computed or hand-crafted.
_DEFAULT_AFFINITY = {
    "affinity_tags": ["balanced"],
    "when_first": (
        "If they react first, the scene takes the natural shape of their "
        "essence - their inner weight drives the party's early posture."
    ),
    "when_last": (
        "If they close the round, they offer the retrospective read - what "
        "the scene meant to them, and how the party should carry the moment "
        "forward."
    ),
}


def get_affinity(persona_id: str) -> dict:
    """Return the affinity record for a persona, or a default shell."""
    return PERSONA_AFFINITIES.get(persona_id, _DEFAULT_AFFINITY)


def get_affinity_for_client(persona_id: str) -> dict:
    """Return a compact, JSON-serializable affinity view suitable for the
    UI to render tooltips + icons + approach-word readouts. Keeps the
    player-facing strings short - no full prose descriptions, which would
    spoil the narrative outcome of a chosen ordering."""
    a = get_affinity(persona_id)
    tags = a.get("affinity_tags", [])[:4]
    icons = [AFFINITY_ICONS.get(t, AFFINITY_ICONS.get("balanced", "\u25cb")) for t in tags]
    # Use the first affinity tag for the approach-word derivation
    primary_tag = tags[0] if tags else "balanced"
    approach_word = APPROACH_WORDS.get(primary_tag, "level")
    return {
        "tags":           tags,
        "icons":          icons,
        "primary_tag":    primary_tag,
        "approach_word":  approach_word,
        "when_first_tag": a.get("when_first_tag", ""),
        "when_last_tag":  a.get("when_last_tag", ""),
    }


def build_affinity_brief(party_ids: list[str]) -> str:
    """Dense multi-line brief of the party's turn-order affinities, for
    injection into the Keeper's session opening prompt."""
    if not party_ids:
        return ""
    lines = []
    for pid in party_ids:
        a = get_affinity(pid)
        tags = ", ".join(a.get("affinity_tags", []))
        lines.append(f"- {pid} [{tags}]")
        lines.append(f"    WHEN FIRST: {a.get('when_first', '')}")
        lines.append(f"    WHEN LAST:  {a.get('when_last', '')}")
    return "\n".join(lines)


def all_affinity_ids() -> list[str]:
    return sorted(PERSONA_AFFINITIES.keys())
