"""
memento_forge.py - Turns a completed cross-persona exchange into a memento item.

Input: (persona_a, persona_b, kind, arc_summary, pair_exchange_count)
Output: a memento dict that satisfies memento_store's schema and is ready
to add_memento().

The forge uses Claude Haiku (via anthropic SDK) to generate the item in one
call. If anthropic isn't available, it falls back to a deterministic local
generator so the system keeps running (degraded but functional).

Item-kind archetypes - each exchange kind has its own physical expression:

    argue    -> weapons, shields, sharp things     (STR/CON flavor)
    agree    -> amulets, clasped hands, tokens     (WIS, harmony)
    converse -> books, scrolls, tomes              (INT, insight)
    banter   -> trinkets, charms, lighter items    (CHA, luck)
    joke     -> masks, props, novelty              (CHA, luck)
    riff     -> mixed bag, whatever emerged        (random)

Rarity floor scales with the pair's exchange count:
    1 exchange   : common (floor) - can still roll up to rare
    2-3          : uncommon floor
    4-6          : rare floor
    7+           : epic floor, can roll legendary
"""

from __future__ import annotations

import json
import os
import re
import random
from typing import Optional

try:
    import anthropic
    _ANTHROPIC_OK = True
except Exception:
    _ANTHROPIC_OK = False


_RARITY_ORDER = ["common", "uncommon", "rare", "epic", "legendary"]

def _rarity_floor(exchange_count: int) -> str:
    if exchange_count <= 1: return "common"
    if exchange_count <= 3: return "uncommon"
    if exchange_count <= 6: return "rare"
    return "epic"

def _bump_rarity(rarity: str, count: int) -> str:
    """Let a pair with a long history occasionally roll higher."""
    try:
        idx = _RARITY_ORDER.index(rarity)
    except ValueError:
        idx = 0
    if count >= 8 and random.random() < 0.3:
        idx = min(idx + 1, len(_RARITY_ORDER) - 1)
    return _RARITY_ORDER[idx]


_SLOT_BY_KIND = {
    "argue":    ["weapon", "shield", "gauntlet"],
    "agree":    ["amulet", "cloak", "ring"],
    "converse": ["book", "scroll", "quill"],
    "banter":   ["trinket", "charm", "flask"],
    "joke":     ["mask", "prop", "novelty"],
    "riff":     ["trinket", "scroll", "charm", "talisman"],
    "agreed":   ["amulet", "cloak", "ring"],  # alias safety
    "argued":   ["weapon", "shield", "gauntlet"],
    "joked":    ["mask", "prop", "novelty"],
}

_STAT_HINT_BY_KIND = {
    "argue":    {"str": +1, "con": +1, "cha": -1},
    "agree":    {"wis": +2},
    "converse": {"int": +2, "wis": +1},
    "banter":   {"cha": +1, "dex": +1},
    "joke":     {"cha": +2},
    "riff":     {},
}

_SYSTEM_PROMPT = """You are a CRPG item forge. You turn a single shared moment between two characters into one physical object that could plausibly have emerged from that moment - a memento, a keepsake, a weapon, a book, a charm. The object is held equally by both characters; it is the thing they would each remember when thinking about the other.

You will receive:
  - the two character names and a one-line sketch of each
  - the kind of exchange (argue / agree / converse / banter / joke / riff)
  - the arc summary of what happened between them
  - the slot archetype (weapon, shield, amulet, book, trinket, mask, etc.)
  - the suggested rarity
  - the sidequest shape (a structured description of WHEN the memento wants to be spent — which encounter type, which positions). You must write a single one-line "sidequest_hint" that describes that condition in the voice of a player clue, NOT as machinery.

Output a single JSON object with EXACTLY these keys:
  name           - string, max 60 chars, evocative item name
  desc           - string, 1-2 sentences, MUST reference something specific from the arc summary (not a generic description)
  slot           - string, one of: main_hand, off_hand, head, chest, legs, feet, amulet, ring, pocket
  rarity         - string, one of: common, uncommon, rare, epic, legendary
  stats          - object, 0-3 stat modifiers like {"str": 1, "wis": -1}. Keys from: str, dex, con, int, wis, cha, luck
  abilities      - array of 0-2 short ability names (like "Verbal Riposte", "Shared Silence", "Closing Argument", "Playful Parry")
  flavor_tag     - string, max 50 chars, a single italicized engraving line (e.g. "forged in disagreement", "kept after she left")
  sidequest_hint - string, max 140 chars. A single line written in the register of a player clue. It tells the player what kind of moment this memento is waiting to be spent in — the shape of the scene that would honor it. Do not write instructions like "equip this and use it in combat." Write in mood and image, e.g. "wants a quiet room after an argument" or "asks to be held when someone speaks the truth." Must match the sidequest_shape you were given but say it in your voice.

Rules:
  - The description must reference a specific detail from the arc summary
  - Argue items trend weapons/shields with STR or CON
  - Agree items trend defensive/amulet with WIS
  - Converse items trend books/scrolls with INT
  - Banter items trend trinkets with CHA
  - Joke items trend masks/props with CHA or luck
  - Higher rarity items can have one small stat penalty alongside their bonuses
  - Return ONLY the JSON object, no prose before or after.

RARITY-GATED COMPLEXITY (CRITICAL - follow these rules exactly):
  - COMMON:    stats: 0-1 modifiers only. abilities: EMPTY ARRAY []. This is a stat stick. No abilities at all.
  - UNCOMMON:  stats: 1-2 modifiers. abilities: exactly 1 passive keyword (e.g. "Shared Silence", "Steady Hand"). No trigger conditions.
  - RARE:      stats: 2-3 modifiers. abilities: 1-2, one MAY have a trigger condition phrased as "when X happens, Y" (e.g. "When this persona crit_fails, heal 1 stress from an ally"). The trigger should reference a game event (crit_fail, crit_success, brink, rest roll).
  - EPIC:      stats: 2-3 modifiers. abilities: exactly 2. One MUST be a camp-time effect (e.g. "Rest rolls get +2 while equipped", "Camp stress drain +1 for the party"). The other can be a trigger or passive.
  - LEGENDARY: stats: 3-4 modifiers. abilities: 2-3. One MUST be a rule-changer that affects the world or the run itself (e.g. "World pressure rises 0.5 slower while equipped", "Sidequest windows on this persona's mementos extend by 2 sessions"). The others can be triggers or camp effects."""


# ─── Phase L: Hybrid sidequest generation ─────────────────────────────────
#
# Deterministic rule logic picks the SHAPE of a memento's sidequest from
# its kind + rarity. The haiku is told what shape and writes the flavorful
# one-line hint. The server assembles both into a full sidequest block
# that's attached to the memento at forge time.

# For each exchange kind, what encounter types feel like "home" for the
# memento that emerged. Rolled randomly from the list at forge time.
_SIDEQUEST_ENCOUNTER_BY_KIND = {
    "argue":    ["combat", "stealth", "moral"],
    "agree":    ["social", "emotional", "moral"],
    "converse": ["puzzle", "social", "arcane"],
    "banter":   ["social", "wilderness", "stealth"],
    "joke":     ["social", "wilderness"],
    "riff":     ["social", "combat", "emotional", "puzzle", "stealth", "moral", "wilderness", "arcane"],
}

# For each kind, which position shape the sidequest prefers. "opens"/"closes"/
# "middle"/"any". For argue, the louder owner opens; for agree, both present
# with no position; for converse, closer with the thinker; for banter, middle.
_SIDEQUEST_POSITION_BY_KIND = {
    "argue":    "opens",
    "agree":    "any",
    "converse": "closes",
    "banter":   "middle",
    "joke":     "any",
    "riff":     "any",
}


def _sidequest_shape_for(kind: str,
                          owners: list[str],
                          rarity: str) -> dict:
    """Deterministic machinery: pick a required-block for the sidequest
    given the exchange kind, the owners, and the rarity. Returns a dict
    suitable for memento_store.live_sidequests_for_scene() checks."""
    kind = (kind or "riff").lower()
    enc_options = _SIDEQUEST_ENCOUNTER_BY_KIND.get(kind, _SIDEQUEST_ENCOUNTER_BY_KIND["riff"])
    encounter_type = random.choice(enc_options)
    position_preference = _SIDEQUEST_POSITION_BY_KIND.get(kind, "any")

    # Both owners must be in party for a sidequest to be live.
    required: dict = {
        "owners_in_party": list(owners),
        "encounter_type":  encounter_type,
    }

    # Pick which owner carries the position requirement. We use the first
    # owner in sorted order as the "lead" so the rule is deterministic-ish.
    # For kinds with an "any" position, skip this.
    if position_preference != "any" and owners:
        lead = sorted(owners)[0]
        required["position"] = {lead: position_preference}

    return required


def _build_sidequest(kind: str,
                     owners: list[str],
                     rarity: str,
                     hint_text: str) -> dict:
    """Assemble a full sidequest block from the deterministic shape +
    the haiku-generated hint text. Called from forge_memento after the
    haiku response is parsed."""
    try:
        import memento_store as _mstore
        window_total = _mstore.default_window_for_rarity(rarity)
    except Exception:
        window_total = 5
    required = _sidequest_shape_for(kind, owners, rarity)
    return {
        "condition_text": (hint_text or "").strip()[:240],
        "required":       required,
        "window_total":   int(window_total),
        "window_used":    0,
        "status":         "pending",
    }


def _call_haiku(user_msg: str) -> Optional[dict]:
    if not _ANTHROPIC_OK:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        # Pull the first {...} JSON block out of the response
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception:
        return None


def _fallback_item(
    persona_a_name: str,
    persona_b_name: str,
    kind: str,
    arc_summary: str,
    rarity: str,
) -> dict:
    """Deterministic local fallback if Haiku is unavailable. Uses template
    strings keyed on kind. Works offline; not as vivid as LLM output."""
    slot_choices = _SLOT_BY_KIND.get(kind, _SLOT_BY_KIND["riff"])
    slot = random.choice(slot_choices)
    stats = dict(_STAT_HINT_BY_KIND.get(kind, {}))
    templates = {
        "argue": (
            f"Cracked Whetstone from {persona_b_name}'s argument",
            f"{persona_a_name} kept it after the exchange. It still has an edge, but not the same one it started with.",
            ["Verbal Parry"],
        ),
        "agree": (
            f"Token of Mutual Ground with {persona_b_name}",
            f"A small, warm thing {persona_a_name} and {persona_b_name} left in each other's hands without comment.",
            ["Shared Resolve"],
        ),
        "converse": (
            f"Folded page marked by {persona_b_name}",
            f"A passage {persona_b_name} copied out for {persona_a_name}. The handwriting is pressed firmly, as if meant.",
            ["Deep Question"],
        ),
        "banter": (
            f"Charm from {persona_b_name}'s pocket",
            f"A light, useless thing {persona_b_name} slipped to {persona_a_name} in the middle of a sentence.",
            ["Playful Aside"],
        ),
        "joke": (
            f"The Punchline {persona_b_name} Wrote Down",
            f"{persona_a_name} keeps it folded. Still makes them laugh, still makes them wince.",
            ["Punchline"],
        ),
        "riff": (
            f"Fragment of Shared Time with {persona_b_name}",
            f"Whatever was in reach when the moment ended. {persona_a_name} does not remember picking it up.",
            [],
        ),
    }
    name, desc, abilities = templates.get(kind, templates["riff"])
    return {
        "name": name,
        "desc": desc,
        "slot": slot,
        "rarity": rarity,
        "stats": stats,
        "abilities": abilities,
        "flavor_tag": f"forged in {kind}",
    }


_HEALING_SYSTEM_PROMPT = """You forge one small object that emerged from a character healing themselves, quietly, alone, during a rest period in camp. The character is carrying real psychic weight from recent scenes and has found a way to soothe themselves - not dramatically, not publicly, just the way people actually tend their own wounds in private.

The item you forge is not a weapon or a shield. It is a small, personal, restorative thing. A kept object. A homemade talisman. A notebook entry transcribed in a steady hand. A ritual object assembled from whatever was to hand. The kind of thing a character would not explain to anyone but would carry forever.

You will receive:
  - the character's name and essence
  - their current stress level and inner weight (what they carry)
  - any current affliction if one is set

Output a single JSON object with EXACTLY these keys:
  name        - string, max 60 chars, a small quiet evocative name
  desc        - 1-2 sentences, present tense, describing the object and the act of its making. MUST reference something specific about this character's inner weight. No abstractions.
  slot        - one of: pocket, amulet, ring (NEVER weapon/shield/armor - healing items live small)
  rarity      - one of: uncommon, rare, epic (healing mementos are never common or legendary)
  stats       - object with 1-2 stat modifiers favoring WIS or CHA. Small values only: +1 or +2. Example: {"wis": +1} or {"cha": +1, "wis": +1}
  abilities   - array of exactly one short ability name framed around self-care, recovery, or inner steadiness. Examples: "Quiet Hands", "The Still Point", "What She Learned To Say To Herself", "The Counting Trick", "A Private Mercy"
  flavor_tag  - max 50 chars, an intimate italic engraving. Something they'd never say aloud. Examples: "for the mornings when I forget", "made to hold the shape of his absence"

Rules:
  - Never loud. Never public. Never triumphant.
  - Reference a specific detail from the character's inner weight.
  - Return ONLY the JSON object."""


def forge_healing_memento(
    persona: dict,
    stress_level: int,
    inner_weight: str = "",
    affliction: Optional[str] = None,
) -> dict:
    """Forge a healing memento - a quiet, personal, restorative item that
    emerges when a character heals themselves in camp. Only minted on a
    critical success during a camp rest roll.

    Returns a memento dict ready for memento_store.add_memento().
    """
    persona_id = persona.get("id", "unknown")
    display    = persona.get("display_name") or persona_id
    essence    = (persona.get("essence") or "")[:280]

    user_msg = (
        f"CHARACTER: {display}\n"
        f"  {essence}\n\n"
        f"CURRENT STRESS: {stress_level} / 20\n"
    )
    if inner_weight:
        user_msg += f"\nINNER WEIGHT (what they carry):\n{inner_weight}\n"
    if affliction:
        user_msg += f"\nCURRENT AFFLICTION: {affliction}\n"
    user_msg += (
        f"\n{display} has found a moment of quiet at the fire and has done "
        f"something small and private that eased the weight. Forge the object "
        f"that emerged from that moment."
    )

    item = None
    if _ANTHROPIC_OK and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=_HEALING_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(
                block.text for block in resp.content
                if getattr(block, "type", "") == "text"
            )
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                item = json.loads(m.group(0))
        except Exception:
            item = None

    if item is None or not isinstance(item, dict) or "name" not in item:
        # Fallback template
        item = {
            "name":       f"A small thing {display} made at the fire",
            "desc":       f"{display} kept it in a pocket. It is not dramatic. It does not need to be explained to anyone.",
            "slot":       "pocket",
            "rarity":     "uncommon",
            "stats":      {"wis": 1},
            "abilities":  ["A Private Mercy"],
            "flavor_tag": "for the quiet ones",
        }

    return {
        "owners":      [persona_id],
        "pair_key":    f"{persona_id}+self",     # self-pair marks this as a healing memento
        "kind":        "healing",
        "name":        str(item.get("name", "Small Quiet Thing"))[:80],
        "desc":        str(item.get("desc", "A kept object."))[:400],
        "slot":        str(item.get("slot", "pocket")),
        "rarity":      str(item.get("rarity", "uncommon")),
        "stats":       {k: int(v) for k, v in (item.get("stats") or {}).items()
                        if isinstance(v, (int, float))},
        "abilities":   [str(a)[:40] for a in (item.get("abilities") or [])][:1],
        "flavor_tag":  str(item.get("flavor_tag", "forged in quiet"))[:60],
        "arc_summary": f"A private moment of self-tending during camp rest at stress level {stress_level}.",
    }


_CAMP_FLAVOR_SYSTEM_PROMPT = """You write a single-sentence vignette describing what a character did during a quiet rest in camp. The vignette is specific, sensory, in present tense, and ROOTED IN THE CHARACTER'S INNER WEIGHT - the vulnerability they carry.

This is not a dramatic moment. It is what they did when no one was watching, when the rest of the party was sleeping or tending their own wounds. It should feel like a single true observation. One sentence, 15-35 words.

Good examples:
  - "Shadowheart knotted three new threads into her prayer cloth tonight, each one a name Shar would have wanted her to forget."
  - "Tesla spent the hour after midnight tracing an equation in the ash at the edge of the fire, erasing it before anyone could see."
  - "Lae'zel did not sleep; she held her sword across her knees and watched the treeline until the stars were wrong."

Bad examples (too generic, don't use):
  - "She sat by the fire and thought about her past."
  - "He rested and recovered his strength."

You will receive the character's name, their essence, their current stress level, and their inner weight. Output ONLY a JSON object with one key:
  { "flavor": "the one sentence here" }"""


def forge_camp_flavor(
    persona: dict,
    stress_level: int,
    inner_weight: str = "",
) -> str:
    """Generate a 1-sentence camp vignette for a persona during rest. Used
    on mid-range camp rolls (11-17) to produce flavor text that the Keeper
    will later weave into the next session's opening brief.

    Returns the flavor string (or a fallback if Haiku is unreachable).
    """
    persona_id = persona.get("id", "unknown")
    display    = persona.get("display_name") or persona_id
    essence    = (persona.get("essence") or "")[:280]

    user_msg = (
        f"CHARACTER: {display}\n"
        f"  {essence}\n\n"
        f"CURRENT STRESS: {stress_level} / 20\n"
    )
    if inner_weight:
        user_msg += f"\nINNER WEIGHT:\n{inner_weight}\n"
    user_msg += f"\nWrite the one-sentence vignette for what {display} did during rest tonight."

    if _ANTHROPIC_OK and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=_CAMP_FLAVOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(
                block.text for block in resp.content
                if getattr(block, "type", "") == "text"
            )
            m = re.search(r"\{.*?\}", text, re.DOTALL)
            if m:
                obj = json.loads(m.group(0))
                flavor = str(obj.get("flavor", "")).strip()
                if flavor:
                    return flavor
        except Exception:
            pass

    # Fallback
    return f"{display} spent the quiet hours tending to something only they understood."


_CAMP_EDGE_SYSTEM_PROMPT = """You forge a temporary edge - a single-use advantage - that a character discovered or crafted during camp rest. The edge is usable in the NEXT SESSION ONLY. It is narrative, specific, tied to this character's strengths, and FITS THEIR INNER WEIGHT.

Think of it as a D&D Inspiration point, or a 'once per rest' ability unlocked by reflection and self-tending. The Keeper will know about it and can weave it into the next scene at the right moment. The player won't necessarily know it's there until it fires.

Good examples:
  - {"name": "The Counting Trick", "desc": "Tesla has worked out a rhythmic counting mnemonic that steadies his hands when the room is hostile. Once this session, when someone dismisses or ridicules him, he can respond with perfect composure instead of spiraling."}
  - {"name": "Shar's Silence", "desc": "Shadowheart found the shape of a prayer she refuses to say aloud. Once this session, she can hear what is NOT being said in a room, catching a lie or a hidden intent no one else would notice."}
  - {"name": "Gith Stalking-Rite", "desc": "Lae'zel spent the night in meditation on an ancient githyanki tracking form. Once this session, she can move through a space without leaving any trace that anyone present would register as a threat."}

You will receive the character's name, essence, stress level, and inner weight. Output ONLY a JSON object with these keys:
  name : max 45 chars, evocative, sounds like a specific discovered thing
  desc : 1-2 sentences, present tense, names what it lets them do ONCE"""


def forge_camp_edge(
    persona: dict,
    stress_level: int,
    inner_weight: str = "",
) -> dict:
    """Generate a temporary edge for a persona - a single-use narrative
    advantage usable in the next session. Used on 18-19 camp rolls.

    Returns {"name", "desc"}.
    """
    persona_id = persona.get("id", "unknown")
    display    = persona.get("display_name") or persona_id
    essence    = (persona.get("essence") or "")[:280]

    user_msg = (
        f"CHARACTER: {display}\n"
        f"  {essence}\n\n"
        f"CURRENT STRESS: {stress_level} / 20\n"
    )
    if inner_weight:
        user_msg += f"\nINNER WEIGHT:\n{inner_weight}\n"
    user_msg += (
        f"\n{display} has found or refined something small during this rest. "
        f"Forge the temporary edge."
    )

    if _ANTHROPIC_OK and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=_CAMP_EDGE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(
                block.text for block in resp.content
                if getattr(block, "type", "") == "text"
            )
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                obj = json.loads(m.group(0))
                name = str(obj.get("name", "")).strip()
                desc = str(obj.get("desc", "")).strip()
                if name and desc:
                    return {"name": name[:60], "desc": desc[:400]}
        except Exception:
            pass

    return {
        "name": f"{display}'s Quiet Advantage",
        "desc": f"Something {display} worked out during the rest. Once this session, it will serve them.",
    }


def forge_memento(
    persona_a: dict,
    persona_b: dict,
    kind: str,
    arc_summary: str,
    pair_exchange_count: int = 1,
) -> dict:
    """Forge a memento for a completed exchange.

    persona_a / persona_b : dict with keys {id, display_name, essence}
    kind                   : one of argue | agree | converse | banter | joke | riff
    arc_summary            : the 1-2 sentence summary that interaction_store stores
    pair_exchange_count    : how many total exchanges this pair has had (incl. this)

    Returns a dict ready for memento_store.add_memento()."""
    kind = (kind or "riff").lower().strip()
    rarity = _bump_rarity(_rarity_floor(pair_exchange_count), pair_exchange_count)
    slot_hint = random.choice(_SLOT_BY_KIND.get(kind, _SLOT_BY_KIND["riff"]))

    # Phase L: pick the sidequest SHAPE deterministically BEFORE calling the
    # haiku, so we can describe the shape to the haiku and ask it to write
    # the flavor hint in its voice. The shape drives the required block;
    # the hint drives the player-facing condition_text.
    a_id = persona_a["id"]
    b_id = persona_b["id"]
    owners_sorted = sorted([a_id, b_id])
    sidequest_required = _sidequest_shape_for(kind, owners_sorted, rarity)
    sidequest_shape_text = _render_sidequest_shape_for_haiku(sidequest_required, owners_sorted, persona_a, persona_b)

    user_msg = (
        f"CHARACTER A: {persona_a.get('display_name', persona_a.get('id', '?'))}\n"
        f"  {(persona_a.get('essence') or '')[:280]}\n\n"
        f"CHARACTER B: {persona_b.get('display_name', persona_b.get('id', '?'))}\n"
        f"  {(persona_b.get('essence') or '')[:280]}\n\n"
        f"EXCHANGE KIND: {kind}\n"
        f"SLOT ARCHETYPE: {slot_hint}\n"
        f"SUGGESTED RARITY: {rarity}\n"
        f"PAIR HISTORY: this is exchange #{pair_exchange_count} between them\n\n"
        f"SIDEQUEST SHAPE: {sidequest_shape_text}\n\n"
        f"ARC SUMMARY: {arc_summary}"
    )
    item = _call_haiku(user_msg)
    if item is None or not isinstance(item, dict) or "name" not in item:
        item = _fallback_item(
            persona_a.get("display_name", persona_a.get("id", "?")),
            persona_b.get("display_name", persona_b.get("id", "?")),
            kind, arc_summary, rarity,
        )

    # Coerce / clamp / default
    hint_text = str(item.get("sidequest_hint", "")).strip()
    if not hint_text:
        # Fallback hint if haiku failed to provide one
        hint_text = _fallback_sidequest_hint(kind, sidequest_required)
    sidequest = _build_sidequest(kind, owners_sorted, rarity, hint_text)

    result = {
        "owners":      owners_sorted,
        "pair_key":    "+".join(owners_sorted),
        "kind":        kind,
        "name":        str(item.get("name", "Fragment"))[:80],
        "desc":        str(item.get("desc", "An object from a shared moment."))[:400],
        "slot":        str(item.get("slot", slot_hint)),
        "rarity":      str(item.get("rarity", rarity)),
        "stats":       {k: int(v) for k, v in (item.get("stats") or {}).items()
                        if isinstance(v, (int, float))},
        "abilities":   [str(a)[:40] for a in (item.get("abilities") or [])][:2],
        "flavor_tag":  str(item.get("flavor_tag", f"forged in {kind}"))[:60],
        "arc_summary": arc_summary or "",
        "status":      "pending",
        "sidequest":   sidequest,
    }
    return result


def _render_sidequest_shape_for_haiku(required: dict,
                                      owners: list[str],
                                      persona_a: dict,
                                      persona_b: dict) -> str:
    """Describe the deterministic sidequest shape to the haiku in English,
    so it can write a flavor hint that honors the shape but says it in
    its own voice."""
    enc = required.get("encounter_type", "any")
    pos = required.get("position") or {}
    id_to_name = {
        persona_a.get("id", ""):  persona_a.get("display_name") or persona_a.get("id", ""),
        persona_b.get("id", ""):  persona_b.get("display_name") or persona_b.get("id", ""),
    }
    parts = [f"This memento wants to be spent in an encounter of type '{enc}' (or any scene of that kind)."]
    if not pos:
        parts.append("Both owners must be present in the party, but neither needs a specific turn position.")
    else:
        for pid, slot in pos.items():
            name = id_to_name.get(pid, pid)
            slot_word = {
                "opens":  "opening the round (first turn)",
                "closes": "closing the round (last turn)",
                "middle": "in a middle turn of the round",
            }.get(slot, "in any position")
            parts.append(f"{name} should be {slot_word}.")
        parts.append("The other owner simply needs to be in the party.")
    return " ".join(parts)


def _fallback_sidequest_hint(kind: str, required: dict) -> str:
    """Generate a minimal flavor hint when the haiku fails to produce one."""
    enc = required.get("encounter_type", "any")
    pos = required.get("position") or {}
    base = {
        "argue":    "wants to be swung in anger",
        "agree":    "wants a quiet hand to hold it",
        "converse": "wants a voice speaking truth",
        "banter":   "wants the room to laugh",
        "joke":     "wants a smile it can carry",
        "riff":     "wants to be taken somewhere unexpected",
    }.get((kind or "riff").lower(), "wants to be spent")
    if pos:
        return f"{base} — in a {enc} scene, by the one who opens or closes it"
    return f"{base} — in a {enc} scene"
