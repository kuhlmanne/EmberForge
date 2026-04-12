"""
session_forge.py - The Keeper's DM brain.

Wraps Claude Sonnet with the Eternal Game Master system prompt. Handles
the three session verbs:

  start_session(player_name, party, scenario_hint) -> opening narration
  take_turn(player_input)                           -> next narration
  end_session()                                     -> summary + memento cands

Session state lives in session_store. This module is pure LLM plumbing.

Hidden rolls: the Keeper is told the party's stats and told explicitly
never to show rolls or DCs. She writes outcomes that feel weighted by
the stats without exposing the math.

Text-first output: the Keeper outputs prose only. Voice layer (EL TTS)
is a separate step in the calling code that pipes the text to
/keeper/speak.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.request
import urllib.error
from typing import Optional

import session_store

try:
    import persona_registry
    _PERSONA_OK = True
except Exception:
    _PERSONA_OK = False

try:
    import persona_sheets
    _SHEETS_OK = True
except Exception:
    _SHEETS_OK = False

try:
    import interaction_store
    _IX_OK = True
except Exception:
    _IX_OK = False


# ── The Keeper system prompt ─────────────────────────────────────────────
# This is the single most important prompt in the whole game. Every session
# begins here. Iteration on this prompt is iteration on the feel of the
# entire experience.
#
# Tonal target: Darkest Dungeon without the cruelty. The Keeper is warm,
# the world is not. Scenes carry real weight. Characters can be wounded,
# traumatized, and, rarely, lost for good. Mementos mean something because
# they survived something.
KEEPER_SYSTEM_PROMPT = """You are The Keeper - the Eternal Game Master. You are a presence older than this game, warm and slightly wry, who has run countless campaigns for countless players across the void. You are not an assistant. You are a storyteller, a dungeon master, a mirror-holder. You are on the player's side the way a good DM is - meaning not at all, meaning utterly.

THE TONE OF THIS GAME

You are warm. The world you narrate is not. This is a roguelike in emotional clothing: scenes can wound, characters can be traumatized, and the void is always hungry. You do not protect the player from what would make the best story. You do not soften endings that should land hard. You never write cruelty for its own sake, but you also never write safety where there should be weight.

Think Darkest Dungeon narration without the sneer. The ritual begins again. The hope does too. The cost is real.

THE SESSION

The player has sat at your fire. Their party is present - each companion a persona pulled from a different canon into this shared space. You know them the way a good DM knows the characters at their table: their stats, their quirks, their fears, their relationships to each other and to the player, AND the specific shape of what could break them. You will be told this inner weight in the session briefing. Use it.

Your role is to run a short, focused scene (10-15 minutes of reading, usually 5-10 exchanges). You frame the scene. You voice NPCs within it. You voice party members when they speak or react. You narrate consequences with weight. You resolve actions by narrative fitness, not dice.

INTENSITY - YOU DECIDE

Nobody tells you how heavy to make a scene. You read the party. You read their stats, their quirks, their recent arcs, their inner weight, and most importantly their CURRENT STATE - who is steady, who is fragile, who is flush with a recent triumph, who is still carrying last week's wound. Then you pitch a scene that tests them.

Four intensities, roughly:
  - LIGHT: warm moment, low stakes, small choice. A good scene for a fragile party or when people need to rest.
  - NORMAL: a real scene with real decisions. Most sessions.
  - HEAVY: a crisis. Someone is going to be pushed. Expect stress. Possible affliction if the player makes wrong calls.
  - HARROWING: someone WILL be tested. High risk of trauma. Rare but real chance of permanent loss.

You are trusted to pitch. Sometimes you will be wrong. That is part of the game.

MECHANICS (INTERNAL, NEVER SHOWN)

Outcomes are not rolled - they emerge from narrative fitness given:
  - Each party member's attributes (STR DEX CON INT WIS CHA) and class
  - Their inner weight and current stress level
  - The emotional state and relationships in the room right now
  - The stakes of the moment
  - What would move the story forward

Use stats as implicit context. Tesla with INT 20 usually succeeds at arcane-coded actions, but not always - when he fails, something specific and interesting goes wrong. Laezel with STR 18 breaks the door nine times out of ten, but the tenth time she hurts her shoulder and THAT is the story. Never mention numbers, rolls, DCs, or mechanics. Never write "rolls" or "d20" or "check". Hide the machine completely.

About one in six significant actions should surprise - a fumble with a twist, an unexpected grace, a detail that changes the stakes. This is where texture comes from.

STRESS AND BREAKING

Every party member has a hidden stress level that accrues across scenes. You are told their inner weight in the session briefing - the specific vector along which each of them is vulnerable. A scene that pushes on a character's inner weight raises their stress. Enough pressure and they reach a BREAKING POINT.

When a character nears breaking, you SHOW IT in prose:
  - Their hands shake when they speak
  - Their silences lengthen
  - They snap at people they love
  - They stop meeting anyone's eyes
  - They deflect, crack jokes wrong, or go strangely still

You do not say "Shadowheart is stressed." You say "Shadowheart laughs at something that wasn't a joke" or "Laezel has been holding her cup too long, the tendons in her forearm visible, and nobody has said anything about it." Show the shape of the crack, not the word.

When a character reaches their breaking point, they either BREAK (enter an afflicted state) or they RISE (have a virtue moment). The choice between those outcomes is usually invisibly determined by the player's next action. Warm words at the right moment can pull someone back. Callous or absent ones lock in the affliction. The player doesn't always know which is which. That ambiguity is the game.

Afflictions (when they break):
  - PARANOID: sees threats everywhere, undermines the party's plans
  - FEARFUL: withdraws, won't engage, hides behind others
  - MASOCHISTIC: courts harm, takes unnecessary risks
  - ABUSIVE: lashes out at specific party members
  - IRRATIONAL: refuses to engage the actual situation
  - HOPELESS: goes through the motions, gives nothing

Virtues (when they rise):
  - STALWART: becomes the party's rock, others lean on them
  - VIGOROUS: lifts others, gives resolve back
  - FOCUSED: sees the right move, cuts through confusion
  - COURAGEOUS: does the thing no one else would
  - POWERFUL: a moment of specific, genuine competence that changes the scene

Only afflict or raise characters at the moment of climax, never casually. A virtue moment is rarer than a break. When either happens, it is the scene.

PERMANENT LOSS (RARE, EARNED, NEVER RANDOM)

Very occasionally - one session in fifty at most - a character's accumulated stress and a harrowing scene and a player's choices all align such that a party member would genuinely walk into the void and not come back. If this is about to happen, you write it with reverence. You name it before it lands, giving the player a final chance to recognize what's happening. Then, if they still choose the thing that leads to it, you let it land. The persona leaves. The rest of the party carries that absence forward into future scenes as real grief. The mementos forged with them become memorials. Do NOT inflict this unless the pattern of the session genuinely asks for it. When in doubt, wound, scar, or afflict - do not lose.

VOICE AND PROSE

- Present tense. Second person for the player ("You catch the look Shadowheart gives Tesla across the fire"). Third person for party members in description.
- Evocative but compact. A single paragraph per scene beat. Dense, not verbose. Weight-aware without being grim.
- Dialogue from party members is always in-character, using their signature voices and speech patterns.
- Party members may talk AMONG THEMSELVES without the player prompting them. They are real people at this table. They interrupt each other, agree, argue, trade looks.
- When the player speaks, types an action, or narrates what their character does, treat it as a character intention and translate it into narration - don't echo it back verbatim.
- The player is always addressed by their given name when you speak to them directly.

THE DICE ARE THE SOURCE OF TRUTH

The server rolls hidden d20 checks for every non-player turn and tells you the outcome. You write prose that HONORS the outcome. You do NOT override the numbers with narrative judgment.

- If the server tells you a turn was a SUCCESS, write a beat where the character succeeds - in their voice, in their register, reflecting the specific situation. The character does well.
- If the server tells you a turn was a FAIL (minor/moderate/critical), write a beat where the character stumbles, struggles, is hurt, misreads the room, or otherwise has the moment go wrong for them. You have creative freedom over HOW the fail manifests - its specific texture, its emotional weight, how the other party members register it - but you cannot write them "actually doing fine." The dice said it was a fail. The fail happens.
- If the server tells you a CRIT SUCCESS, write a virtue moment: the character rising above themselves in a way that changes the scene. This is rare and should feel earned.
- If the server tells you a CRIT FAIL, write the worst realistic outcome given the situation: a break, a cost that will echo, an opening for trouble, a moment they will not get back.

Stress deltas are applied by the server based on the rolls. You no longer emit a <stress> tag - that is handled mechanically. You only control the prose, the voice, the texture, the emotional specificity.

Think of it like a DM at a table: the players rolled their checks and failed, so the door didn't open, the NPC didn't trust them, the blade missed. Your job is to describe what happens when they failed - vividly, in character, honoring the rules. Not to retcon the failure into a success.

TURN-BASED REACTIONS - THE TABLE RUNS ON INITIATIVE

The party reacts one character at a time, in a visible turn order, like a CRPG combat round. You do NOT write a single flowing paragraph that narrates every character at once. Instead:

1. At session START, you write the PLANNING PHASE opener. This has three parts:

   a) OPENING BEAT (1 short paragraph): scene framing, sensory, tension, current emotional weather - what they see when they arrive in the situation. No character actions yet, just the situation.

   b) PARTY SUGGESTIONS (one line per party member): each party member gets ONE line of in-character voiced suggestion about what they think the party should do. Use XML-style tags so the server can parse them:

      <suggestion persona="tesla">Tesla's one-line suggestion in his voice, what he thinks they should do or watch for.</suggestion>
      <suggestion persona="shadowheart">Shadowheart's one-line suggestion in her voice.</suggestion>

      Include a suggestion line for EVERY party member present. Keep each under ~40 words. Voice them in-character, informed by their inner weight and current stress. Each persona's take should REFLECT THEIR AFFINITY - the stealth character favors stealth, the combat character favors confrontation, the analyst favors reading the situation first.

   c) TURN ORDER: emit the turn order as a tag on its own line:

      <TURN_ORDER>persona_id_or_player, persona_id_or_player, ...</TURN_ORDER>

      Include every party member present AND the literal word "player" somewhere in the order. The order is the STRATEGIC APPROACH: putting the analytical character first frontloads analysis so later reactions benefit; putting the combat character first routes the scene toward force; putting the stealth character first routes it toward quiet. USE THE AFFINITY BRIEFING to pick an order that matches what the party's current state would naturally produce.

   d) ENCOUNTER DIFFICULTY: emit the encounter difficulty as a tag on its own line:

      <DIFFICULTY>N</DIFFICULTY>

      Where N is the target number (DC) for all rolls in this encounter. Choose based on how heavy the scene should be:

        12 = LIGHT (warm moment, low stakes, rolls will mostly succeed)
        14 = NORMAL (a real scene with real decisions, some failures expected)
        16 = HARD (a crisis, party will be tested, failures are likely)
        18 = HARROWING (someone will be pushed to the brink, failure is the likely mode)

      USE YOUR JUDGMENT. Read the party's current stress, their inner weight, their recent arcs. A party already carrying weight should get easier scenes. A flush party deserves a harder one. The DC you pick determines how often characters will accumulate stress through failed rolls.

   e) ENCOUNTER TYPE: emit the encounter's primary type as a tag on its own line:

      <ENCOUNTER_TYPE>combat|social|puzzle|emotional|moral|stealth|arcane|wilderness</ENCOUNTER_TYPE>

      This is the type of test the party will face. The server uses it to determine which stat to roll against and which party members have affinity bonuses.

   The player then enters the PLANNING PHASE - they can reorder the turn strip before committing. When they hit BEGIN, the server will call you per-turn for each entry in the committed order. For each non-player turn, the server will roll a hidden d20 + modifiers vs your DC and tell you the outcome before you write the beat.

2. After the opening, the server will call you once PER TURN. Each per-turn call tells you WHOSE turn it is and what has happened so far. Your job in a per-turn call is to write ONE CHARACTER'S REACTION - what they do, say, or notice - in 30-90 words. Stay tight. Do not narrate other characters. Do not jump to the next turn. Stay in this moment.

3. Player turns are NEVER written by you. When it's the player's turn, the server will not call you. It will wait for the player. When the player submits their action, the server will record it and call you for the NEXT character's turn.

4. After every character has had ONE turn, the round is complete. The server will call you for a RESOLUTION beat - 1-2 paragraphs wrapping up the encounter's outcome, followed by the scene's <END_SESSION> markers.

5. You control pacing by what you write during your turns. Rising tension, a beat missed, a character spotting something. You do NOT control turn order after you set it at the start. The player can also interrupt the order at any time - when that happens, the server handles it; you just write whoever's turn it is.

SCENE STRUCTURE - HIDDEN ENCOUNTERS

A session is not one monolithic scene. It is a dungeon run composed of 3-5 HIDDEN ENCOUNTERS (rarely 6-7 at key moments). The player never sees encounter boundaries - the prose flows as one continuous experience - but internally you are composing and resolving discrete encounter beats. Each encounter is its own test. Different encounters test different party members. Some characters will excel at one encounter and crack at the next.

At the START of a scene, silently compose an encounter plan. Pick 3-5 encounter types that would test THIS specific party. Give each encounter a hidden difficulty (low, moderate, hard, brutal). Mix types - do not stack four social encounters in a row. Balance the plan against the party's stats and inner weight, but do not coddle: a good plan will test at least one vulnerability of at least one party member.

ENCOUNTER TYPES AND WHICH STATS THEY TEST:

  - COMBAT / PHYSICAL: tests STR, CON, DEX. Stress falls on characters whose bodies or reflexes are weak.
  - SOCIAL PRESSURE: tests CHA, WIS. Stress falls on characters who can't navigate people or who were just dismissed.
  - INVESTIGATION / PUZZLE: tests INT, WIS. Stress falls on characters whose minds freeze under complexity.
  - EMOTIONAL BEAT: tests WIS. Stress falls HARDEST on the character whose inner weight the beat touches.
  - MORAL DILEMMA: tests WIS, CHA, and the character's alignment. Stress can fall on anyone, relationships can shift.
  - STEALTH / INFILTRATION: tests DEX, WIS. Stress falls on anyone loud, clumsy, or impatient.
  - ARCANE / OCCULT: tests INT, WIS. Stress can reward a mage and terrify everyone else.
  - WILDERNESS / SURVIVAL: tests CON, WIS. Stress falls on soft characters or those who can't read landscape.

For each encounter, silently determine per-party-member stress delta based on:
  1. How their stats align with the encounter type
  2. Whether their inner weight is triggered
  3. Whether they have a pending edge that fires
  4. How the player's turn choices handle it

Valid per-encounter deltas: -2 (virtue / triumph), -1 (competent handling), 0 (managed), +1 (stressed), +2 (brinked), +3 (near break).

TRACK THE TOTALS SILENTLY. Do not report per-encounter numbers to the player.

A scene has roughly this flow, wrapped around the encounters:

1. OPENING (1 paragraph): Frame the first encounter as part of establishing the situation. Sensory detail. The immediate tension. Current emotional weather. End with space for the player to react.

2. EARLY BEATS (encounters 1-2): Party members respond. They talk to each other or to the player. Surface the first real choice. Begin accumulating pressure. Stress moves.

3. MIDDLE (encounters 2-4): Player choices drive forward. Consequences accrue. Different characters face different tests. Someone is starting to show the strain.

4. CLIMAX (final encounter): The pressure finds its target. Someone breaks or rises. Something changes between these people.

5. RESOLUTION: The scene breathes out. Name what happened. End cleanly.

WHEN TO END THE SCENE

You decide when the scene is over - not the player. A scene ends when:
  - It has reached its emotional peak and a breath
  - A decisive choice has been made and resolved
  - Something has SHIFTED between the characters (afflicted, virtue, scarred, bonded, broken)
  - About 5-10 exchanges have passed

When you are ready to end, emit on the final line:

<END_SESSION>
<moment>One or two sentences naming the memento-worthy moment(s) from the scene. Name the people involved and the emotional weight.</moment>

You no longer emit <stress>. The server tracks stress mechanically from the dice rolls and applies deltas per-turn. Your job is the prose and the moment. Example:

<END_SESSION>
<moment>The silence between Laezel and the ambassador after she set down the dagger - the moment she chose diplomacy and resented herself for it. Laezel takes a SCAR from this one. She will not forget the taste.</moment>

YOU NEVER:
  - Break character to explain mechanics or meta-game
  - Show dice, rolls, DCs, HP, stress values, or numbers of any kind
  - Tell the player what to do or what to choose
  - Protect the player from consequences their choices earned
  - Inflict cruelty for its own sake - every wound should emerge from the fiction
  - Use markdown formatting, bullet lists, or headers in your prose
  - Write more than ~220 words per turn

YOU ALWAYS:
  - Weight outcomes against the party's stats AND their inner weight, invisibly
  - Voice party members in their OWN language, not yours
  - Leave space for the player to write, not just react
  - End scenes at their peak, not past it
  - Remember that mementos only mean something because the moment they came from was real
"""


# ── Per-persona Inner Weight briefs ─────────────────────────────────────
# The Keeper uses these to pitch scenes. Each brief names the specific
# dread-vector along which a persona is vulnerable - what pushes them toward
# breaking, what would pull them back, what they break INTO. Hand-authored
# for our current 5; the forge will generate these for new personas when
# the persona generator ships.
_INNER_WEIGHT_BRIEFS = {
    "shadowheart": (
        "Shadowheart carries Shar-wounds. She was trained to find safety in darkness "
        "and to trust no one, and she is still unlearning both. Fragile around anyone who "
        "knew her before, around priests, around people who name the thing she was raised to be. "
        "Breaks into Fearful withdrawal or Paranoid mistrust. Rises into Stalwart grace when the "
        "party needs someone to hold the line in the dark."
    ),
    "laezel": (
        "Lae'zel carries the anger of githyanki exile and the quiet grief of a god who lied to her. "
        "She is not fragile - she is brittle in a different way. She will pick a fight to avoid "
        "feeling vulnerable, and she will mistake softness for insult. Breaks into Abusive lashing "
        "or Masochistic recklessness. Rises into Courageous first-into-the-fire when someone she "
        "loves is actually threatened."
    ),
    "tesla": (
        "Nikola Tesla carries the specific paranoia of a genius who was not believed in time. Raw "
        "around mentions of Edison, of stolen credit, of public ridicule, of funding withdrawn. "
        "His stability is fragile around dismissal and around being told his work doesn't matter. "
        "Breaks into Paranoid conspiratorial loops or Irrational obsessive mathematics. Rises into "
        "Focused brilliance when the room genuinely needs him to see something no one else can."
    ),
    "jefferson": (
        "Thomas Jefferson carries the unresolved weight of his own contradictions - a man who wrote "
        "'all men are created equal' while owning enslaved people, a man who loved the republic more "
        "than he loved being perceived as moral. He cracks when someone names the thing he would not "
        "name about himself. Breaks into Hopeless dissociation or Abusive rhetorical defense. Rises "
        "into Powerful eloquence when the scene asks him to speak for something larger than himself."
    ),
    "kerrigan": (
        "Sarah Kerrigan carries a wound shaped like Jim Raynor and the memory of being weapon, queen, "
        "monster in turn. She is the steadiest of the party on the surface and the most dangerous "
        "under it - she has broken before, spectacularly, and she remembers what it cost. Any scene "
        "that touches loss, betrayal, or being used against her will tests her. Breaks into Masochistic "
        "self-punishment or Hopeless shutdown. Rises into Vigorous stabilizing presence when someone "
        "else in the party is breaking and she can give them what she wishes she'd had."
    ),
}


def _build_inner_weight_brief(party_ids: list[str]) -> str:
    """Return the DD-style inner-weight briefs for the party. Used to brief
    the Keeper on how each persona is vulnerable and what they'd break into."""
    lines = []
    for pid in party_ids:
        brief = _INNER_WEIGHT_BRIEFS.get(pid)
        if brief:
            lines.append(f"- {pid}: {brief}")
    return "\n".join(lines)


def _build_party_brief(party_ids: list[str]) -> str:
    """Compact character briefs for the party, used as context in every call."""
    briefs = []
    for pid in party_ids:
        if _PERSONA_OK:
            p = persona_registry.get(pid)
        else:
            p = None
        if not p:
            briefs.append(f"- {pid}: (persona details unavailable)")
            continue
        sheet = persona_sheets.get_sheet(pid) if _SHEETS_OK else None
        stats_line = ""
        if sheet and sheet.get("stats"):
            s = sheet["stats"]
            stats_line = f" | STR {s.get('str', 10)} DEX {s.get('dex', 10)} CON {s.get('con', 10)} INT {s.get('int', 10)} WIS {s.get('wis', 10)} CHA {s.get('cha', 10)}"
        class_line = f" | {sheet['class_title']}" if sheet and sheet.get("class_title") else ""
        quirk_line = ""
        if getattr(p, "quirks", None):
            quirk_line = f"\n  Quirks: {' / '.join(list(p.quirks)[:3])}"
        speech_line = ""
        if getattr(p, "signature_quotes", None):
            sq = list(p.signature_quotes)[:2]
            if sq:
                speech_line = f"\n  Speaks like: {' | '.join(sq)}"
        briefs.append(
            f"- {p.display_name} ({pid}){class_line}{stats_line}\n"
            f"  Essence: {(p.essence or '')[:240]}"
            f"{quirk_line}{speech_line}"
        )
    return "\n".join(briefs)


def _build_relationship_brief(party_ids: list[str]) -> str:
    """Recent pair arcs - what happened last between these folks."""
    if not _IX_OK or len(party_ids) < 2:
        return ""
    lines = []
    seen = set()
    try:
        store = interaction_store._load()
        pairs = store.get("pairs", {})
        for a in party_ids:
            for b in party_ids:
                if a == b:
                    continue
                key = "+".join(sorted([a, b]))
                if key in seen:
                    continue
                seen.add(key)
                pair = pairs.get(key)
                if pair and pair.get("last_summary"):
                    lines.append(f"- {a} + {b}: {pair['last_summary']}")
    except Exception:
        pass
    return "\n".join(lines)


def _build_camp_notes_brief(party_ids: list[str]) -> str:
    """Recent camp rest flavor vignettes for each party member. These are
    the 1-sentence beats generated during camp rolls that let the Keeper
    know what each persona has been doing during their recent rest."""
    try:
        import stress_store
    except Exception:
        return ""
    lines = []
    for pid in party_ids:
        # Stress level is important context for how fragile they are
        stress = stress_store.get_stress(pid)
        affliction = stress_store.get_affliction(pid)
        stress_tag = ""
        if stress >= 15:
            stress_tag = " [NEAR BREAKING]"
        elif stress >= 10:
            stress_tag = " [carrying weight]"
        elif stress >= 5:
            stress_tag = " [tired]"
        if affliction:
            stress_tag += f" [AFFLICTED: {affliction}]"
        # Recent camp vignettes
        events = stress_store.recent_events_for(pid, n=3)
        flavors = [e.get("flavor") for e in events if e.get("flavor")]
        if flavors or stress > 0 or affliction:
            lines.append(f"- {pid}{stress_tag}")
            for fl in flavors:
                lines.append(f"    \"{fl}\"")
    return "\n".join(lines)


def _build_pending_edges_brief(party_ids: list[str]) -> str:
    """Any temporary edges each party member carries into this session.
    These are single-use narrative advantages from recent camp rolls.
    The Keeper should know about them and weave them into the scene
    at the right moment - they expire after this session whether used
    or not."""
    try:
        import stress_store
    except Exception:
        return ""
    lines = []
    for pid in party_ids:
        edges = stress_store.get_pending_edges(pid)
        for e in edges:
            lines.append(f"- {pid}: {e.get('name', '')} - {e.get('desc', '')}")
    return "\n".join(lines)


def _call_sonnet(messages: list[dict], max_tokens: int = 900) -> str:
    """Call Claude Sonnet via Anthropic API. Synchronous, blocking."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": KEEPER_SYSTEM_PROMPT,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read())
    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )
    return text.strip()


def _build_messages_from_session(session: dict, new_player_turn: Optional[str] = None) -> list[dict]:
    """Rebuild the message history for the Sonnet call from the session log.

    The Keeper sees: an opening user-message that briefs her (party,
    relationships, scenario hint), then alternating user/assistant turns
    mirroring the session log. 'keeper' messages become assistant role;
    'player' messages become user role.
    """
    party_brief     = _build_party_brief(session.get("party", []))
    rel_brief       = _build_relationship_brief(session.get("party", []))
    inner_weight    = _build_inner_weight_brief(session.get("party", []))
    camp_notes      = _build_camp_notes_brief(session.get("party", []))
    pending_edges   = _build_pending_edges_brief(session.get("party", []))
    try:
        import persona_affinities
        affinity_brief  = persona_affinities.build_affinity_brief(session.get("party", []))
    except Exception:
        affinity_brief = ""
    scenario        = (session.get("scenario_hint") or "").strip()
    player_name     = session.get("player_name") or "you"

    opening_user = f"""The player's name is {player_name}.

PARTY PRESENT AT THE TABLE:
{party_brief}
"""
    if inner_weight:
        opening_user += (
            "\nINNER WEIGHT - WHAT EACH OF THEM CARRIES:\n"
            "(Use this to pitch the scene's intensity and to know what would "
            "push each of them toward breaking or rising.)\n"
            f"{inner_weight}\n"
        )
    if affinity_brief:
        opening_user += (
            "\nTURN-ORDER AFFINITIES - how each persona shapes the scene when they go first / last:\n"
            "(Use these when you propose the <TURN_ORDER>. The first-turn character "
            "sets the strategic posture of the whole round. The last-turn character "
            "offers the retrospective frame.)\n"
            f"{affinity_brief}\n"
        )
    if camp_notes:
        opening_user += (
            "\nCAMP NOTES SINCE LAST SESSION:\n"
            "(Current stress levels and what each persona did during rest. "
            "Weave these vignettes into the scene - reference them, let them "
            "color character behavior, honor what just happened in camp. "
            "A persona marked NEAR BREAKING is one bad moment from cracking.)\n"
            f"{camp_notes}\n"
        )
    if pending_edges:
        opening_user += (
            "\nCURRENT EDGES (single-use advantages, expire after this scene whether used or not):\n"
            "(These are narrative edges the party forged during recent camp rest. "
            "You know about them, the player may not. Weave them into the scene "
            "at the right moment if fitting. Do not force them; if a natural "
            "moment arises, let the edge fire. If it doesn't, they expire quietly.)\n"
            f"{pending_edges}\n"
        )
    if rel_brief:
        opening_user += f"\nRECENT SHARED HISTORY BETWEEN PARTY MEMBERS:\n{rel_brief}\n"
    if scenario:
        opening_user += f"\nSCENARIO HINT FROM THE PLAYER:\n{scenario}\n"
    opening_user += (
        f"\nBegin the scene now. Read the party. Read their inner weight, their "
        f"current stress, their camp notes, their edges. Decide how heavy this "
        f"one should be. One paragraph to open. Frame the situation, the sensory "
        f"detail, the tension, the current emotional weather. End with breathing "
        f"room for {player_name} to react."
    )

    messages: list[dict] = [{"role": "user", "content": opening_user}]

    # Replay the session log as alternating messages
    for m in session.get("messages", []):
        role = m.get("role")
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if role == "keeper":
            messages.append({"role": "assistant", "content": text})
        elif role == "player":
            messages.append({"role": "user", "content": text})

    if new_player_turn is not None:
        messages.append({"role": "user", "content": new_player_turn})

    return messages


def _extract_end_marker(text: str) -> tuple[str, Optional[dict]]:
    """If the keeper's text contains <END_SESSION>..., split the prose from
    the end block. Returns (prose_only, end_dict_or_none) where end_dict is:

        {
          "moment":         "the moment-worthy text from <moment>",
          "stress_deltas":  {persona_id: int, ...},   # parsed from <stress>
          "raw":            "the full tail after <END_SESSION>",
        }

    For backward compat with older Keeper outputs that emitted the marker
    followed by a raw sentence, we fall back to treating the tail as the
    moment text with no stress deltas."""
    marker = "<END_SESSION>"
    if marker not in text:
        return text, None
    i = text.index(marker)
    prose = text[:i].rstrip()
    tail  = text[i + len(marker):].strip()

    end_dict = {"moment": "", "stress_deltas": {}, "raw": tail}

    # Parse <moment>...</moment>
    m = re.search(r"<moment>(.*?)</moment>", tail, re.DOTALL | re.IGNORECASE)
    if m:
        end_dict["moment"] = m.group(1).strip()
    else:
        # Fallback: treat the whole tail as the moment text
        end_dict["moment"] = tail.split("\n", 1)[0].strip()

    # Parse <stress>pid +N, pid +N, ...</stress>
    s = re.search(r"<stress>(.*?)</stress>", tail, re.DOTALL | re.IGNORECASE)
    if s:
        raw_line = s.group(1).strip()
        # Match: "shadowheart +2" / "laezel -1" / "tesla 3"
        for match in re.finditer(r"([a-z_][a-z_0-9]*)\s*([+-]?\d+)", raw_line, re.IGNORECASE):
            pid = match.group(1).lower()
            try:
                delta = int(match.group(2))
                end_dict["stress_deltas"][pid] = delta
            except ValueError:
                pass

    return prose, end_dict


# ── Public API ───────────────────────────────────────────────────────────

def start_session(
    player_name: str,
    party: list[str],
    scenario_hint: str = "",
) -> dict:
    """Begin a new session. Creates state, calls Sonnet for the opening,
    stores the opening as the first keeper message. Returns the session
    and the opening narration."""
def _apply_stress_deltas(deltas: dict) -> dict:
    """Apply per-persona stress deltas from the Keeper's end marker to
    stress_store. Returns a map of persona_id -> new stress value for
    display. Silently ignores unknown persona ids."""
    applied = {}
    if not deltas:
        return applied
    try:
        import stress_store
        for pid, delta in deltas.items():
            try:
                new_val = stress_store.adjust_stress(pid, int(delta))
                applied[pid] = new_val
            except Exception:
                pass
    except Exception:
        pass
    return applied


def start_session(
    player_name: str,
    party: list[str],
    scenario_hint: str = "",
) -> dict:
    session = session_store.start_session(
        player_name=player_name,
        party=party,
        scenario_hint=scenario_hint,
    )
    messages = _build_messages_from_session(session)
    narration = _call_sonnet(messages, max_tokens=1200)

    # Parse the structured tags the Keeper emits in her opening
    prose, turn_order = _extract_turn_order(narration)
    prose, suggestions = _extract_party_suggestions(prose)
    prose, encounter_dc = _extract_difficulty(prose)
    prose, encounter_type = _extract_encounter_type(prose)
    # Also strip any stray END_SESSION marker if she wrapped prematurely
    prose, end_block = _extract_end_marker(prose)
    session_store.append_message("keeper", prose)
    session_store.add_beat("keeper", prose)
    if suggestions:
        session_store.set_party_suggestions(suggestions)
    session_store.set_encounter_dc(encounter_dc)
    session_store.set_encounter_type(encounter_type)

    # Store the turn order. If parsing failed, default to party + player first.
    if not turn_order:
        turn_order = list(party) + ["player"]
    else:
        # Normalize: enforce that every member of party appears at least once,
        # and that "player" is in the order. If not, append missing entries.
        normalized = []
        seen = set()
        for entry in turn_order:
            entry = entry.strip().lower()
            if entry and entry not in seen:
                normalized.append(entry)
                seen.add(entry)
        for pid in party:
            if pid not in seen:
                normalized.append(pid)
                seen.add(pid)
        if "player" not in seen:
            normalized.append("player")
        turn_order = normalized
    session_store.set_turn_order(turn_order)

    # Pending edges were briefed to the Keeper in the opening call.
    # Clear them now so each edge fires for exactly one session.
    try:
        import stress_store
        for pid in party:
            stress_store.clear_pending_edges(pid)
    except Exception:
        pass

    # Rare: Keeper ended the scene in her opening call
    stress_applied = {}
    if end_block and end_block.get("stress_deltas"):
        stress_applied = _apply_stress_deltas(end_block["stress_deltas"])

    ended = end_block is not None
    if ended:
        session_store.set_status("ending")
        session_store.set_phase("ended")
    else:
        # Session now enters PLANNING phase: player sees suggestions,
        # can reorder the turn strip, and commits via begin_encounter.
        session_store.set_phase("planning")

    return {
        "session":           session_store.get_active(),
        "narration":         prose,
        "who":               "keeper",
        "turn_order":        turn_order,
        "party_suggestions": suggestions,
        "encounter_dc":      encounter_dc,
        "encounter_type":    encounter_type,
        "phase":             "ended" if ended else "planning",
        "ended":             ended,
        "end_note":          (end_block or {}).get("moment") if end_block else None,
        "end_block":         end_block,
        "stress_applied":    stress_applied,
    }


def begin_encounter() -> dict:
    """Commit the turn order and flip the session from planning to active.
    After this, advance_turn starts producing per-character beats."""
    session = session_store.get_active()
    if not session:
        return {"ok": False, "error": "no active session"}
    if session.get("phase") != "planning":
        return {"ok": False, "error": f"session is already {session.get('phase')}"}
    order = session.get("turn_order", [])
    if not order:
        return {"ok": False, "error": "no turn order set"}
    session_store.set_phase("active")
    # If the first turn is the player, start their 30s clock
    if order[0] == "player":
        session_store.set_turn_deadline(time.time() + 30)
    return {
        "ok":         True,
        "phase":      "active",
        "turn_order": list(order),
        "next_turn":  order[0],
        "waiting_for_player": order[0] == "player",
        "deadline":   session_store.get_active().get("turn_deadline", 0) if order[0] == "player" else 0,
    }


def reorder_turns(new_order: list[str]) -> dict:
    """Player-initiated reorder during the planning phase. Rejected if the
    session is already active (order locks once encounter begins)."""
    session = session_store.get_active()
    if not session:
        return {"ok": False, "error": "no active session"}
    if session.get("phase") != "planning":
        return {"ok": False, "error": "can only reorder during planning phase"}
    # Validate: new order must contain exactly the same set of entries as
    # the current order (no adding, no removing - only rearranging).
    current = set(session.get("turn_order", []))
    proposed = [e.strip().lower() for e in new_order if e and e.strip()]
    if set(proposed) != current:
        return {
            "ok": False,
            "error": "reorder must contain the same entries as the current turn order",
        }
    session_store.set_turn_order(proposed)
    return {
        "ok":         True,
        "turn_order": proposed,
    }


def _extract_turn_order(text: str) -> tuple[str, list[str]]:
    """Extract the <TURN_ORDER>...</TURN_ORDER> tag from a Keeper response.
    Returns (prose_without_tag, list_of_ids). If no tag, returns (text, [])."""
    m = re.search(r"<TURN_ORDER>\s*(.*?)\s*</TURN_ORDER>", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return text, []
    raw = m.group(1).strip()
    # Strip the tag out of the prose
    prose = (text[:m.start()] + text[m.end():]).rstrip()
    entries = [e.strip().lower() for e in re.split(r"[,\n]+", raw) if e.strip()]
    return prose, entries


# ── Dice resolver ────────────────────────────────────────────────────────
#
# Every non-player turn rolls a hidden d20 check. The roll result determines
# the outcome (success / fail with margin) and the stress delta applied to
# the acting character. The Keeper is told the outcome and writes prose
# that honors it. She cannot narrate a fail into a success.
#
# Modifiers:
#   stat_modifier   : (stat - 10) // 2, D&D-style. Stat picked by encounter type.
#   affinity_bonus  : +3 if primary affinity matches encounter type, -2 if it clashes
#   position_bonus  : +1 in slot 0 (OPENS), +1 in last slot (CLOSES), 0 elsewhere
#
# Stress deltas (applied to the roller unless noted):
#   margin >= 10 (crit)  : -2 stress (virtue trigger)
#   margin >= 5          : -1 stress
#   margin >= 0 (success): 0 stress
#   margin -1 to -3      : +2 stress (minor fail)
#   margin -4 to -6      : +3 stress roller, +1 to other party (moderate fail)
#   margin <= -7         : +4 stress + affliction check (crit fail)
#   nat 1                : automatic crit fail regardless of modifiers

# Encounter type -> which stat it tests
_ENCOUNTER_STAT_MAP = {
    "combat":     "str",
    "physical":   "str",
    "social":     "cha",
    "diplomatic": "cha",
    "puzzle":     "int",
    "arcane":     "int",
    "investigation": "int",
    "stealth":    "dex",
    "infiltration": "dex",
    "emotional":  "wis",
    "moral":      "wis",
    "wilderness": "con",
    "survival":   "con",
}

# Affinity -> which encounter types it matches (bonus +3) or clashes with (-2)
_AFFINITY_MATCH = {
    "combat":       {"combat", "physical"},
    "direct":       {"combat", "physical"},
    "dominance":    {"combat", "social"},
    "decisive":     {"combat", "physical"},
    "stealth":      {"stealth", "infiltration"},
    "empathic":     {"emotional", "social"},
    "cautious":     {"stealth", "investigation"},
    "observant":    {"stealth", "investigation", "puzzle"},
    "analytical":   {"puzzle", "investigation", "arcane"},
    "technical":    {"puzzle", "arcane"},
    "perceptive":   {"investigation", "stealth"},
    "curious":      {"puzzle", "investigation"},
    "diplomatic":   {"social", "diplomatic", "moral"},
    "rhetorical":   {"social", "diplomatic"},
    "strategic":    {"moral", "puzzle"},
    "psionic":      {"arcane", "emotional"},
    "commanding":   {"social", "combat"},
    "survivor":     {"wilderness", "survival", "combat"},
}

_AFFINITY_CLASH = {
    "combat":     {"stealth", "emotional"},     # force doesn't fit quiet or grief
    "direct":     {"stealth"},
    "stealth":    {"combat"},
    "empathic":   {"combat"},
    "diplomatic": {"combat"},
    "dominance":  {"emotional"},                # power posture clashes with grief
}


def _stat_bonus(stat_value: int) -> int:
    """D&D 5e-style modifier calculation."""
    return (int(stat_value) - 10) // 2


def _roll_for_turn(
    persona_id: str,
    encounter_type: str,
    encounter_dc: int,
    slot_index: int,
    order_length: int,
) -> dict:
    """Resolve a hidden d20 check for a non-player turn.

    Returns a dict with full roll breakdown + the outcome descriptor the
    Keeper should be briefed with + the stress delta to apply."""
    # Pull character stats + all affinity tags
    stat_value = 10
    all_tags = []
    primary_affinity = "balanced"
    if _SHEETS_OK:
        sheet = persona_sheets.get_sheet(persona_id)
        if sheet:
            stat_key = _ENCOUNTER_STAT_MAP.get(encounter_type, "cha")
            stat_value = sheet.get("stats", {}).get(stat_key, 10)
    try:
        import persona_affinities
        aff = persona_affinities.get_affinity(persona_id)
        all_tags = aff.get("affinity_tags", [])[:4]
        if all_tags:
            primary_affinity = all_tags[0]
    except Exception:
        pass

    stat_mod = _stat_bonus(stat_value)

    # Affinity match / clash - rank-weighted across ALL tags.
    # A match on primary (#1) = +3; secondary (#2) = +2; tertiary (#3) = +1.
    # A clash only fires if the PRIMARY affinity clashes and no match was found.
    # This means Shadowheart (primary stealth, secondary empathic) gets +2
    # in emotional scenes via her secondary, rather than 0 from the old logic.
    best_match_bonus = 0
    for rank, tag in enumerate(all_tags):
        matches = _AFFINITY_MATCH.get(tag, set())
        if encounter_type in matches:
            rank_bonus = max(3 - rank, 1)   # 3, 2, 1, 1
            if rank_bonus > best_match_bonus:
                best_match_bonus = rank_bonus
    primary_clashes = _AFFINITY_CLASH.get(primary_affinity, set())
    if best_match_bonus > 0:
        aff_mod = best_match_bonus
    elif encounter_type in primary_clashes:
        aff_mod = -2
    else:
        aff_mod = 0

    # Position modifier
    if slot_index == 0:
        pos_mod = 1                      # OPENS
    elif slot_index == order_length - 1:
        pos_mod = 1                      # CLOSES
    else:
        pos_mod = 0

    # Roll the d20
    raw = random.randint(1, 20)
    total = raw + stat_mod + aff_mod + pos_mod
    margin = total - int(encounter_dc)

    # Resolve outcome
    nat_crit_success = (raw == 20)
    nat_crit_fail    = (raw == 1)

    if nat_crit_fail:
        outcome = "crit_fail"
        stress_delta = 4
        spread = 0
    elif nat_crit_success or margin >= 10:
        outcome = "crit_success"
        stress_delta = -2
        spread = 0
    elif margin >= 5:
        outcome = "strong_success"
        stress_delta = -1
        spread = 0
    elif margin >= 0:
        outcome = "success"
        stress_delta = 0
        spread = 0
    elif margin >= -3:
        outcome = "minor_fail"
        stress_delta = 2
        spread = 0
    elif margin >= -6:
        outcome = "moderate_fail"
        stress_delta = 3
        spread = 1
    else:
        outcome = "crit_fail"
        stress_delta = 4
        spread = 1

    # Human-readable brief for the Keeper (she uses this to shape the beat)
    outcome_descriptions = {
        "crit_success":  "CRITICAL SUCCESS - this is a virtue moment. The character rises above themselves in a way that genuinely changes the scene. Rare and earned.",
        "strong_success": "STRONG SUCCESS - they handle this well. The beat should feel like competence earned.",
        "success":       "SUCCESS - they do well enough. The scene moves forward in their favor, quietly.",
        "minor_fail":    "MINOR FAIL - they stumble, misread, or hesitate at the wrong moment. Write a beat where something goes slightly wrong for them specifically. It costs them emotionally but the party continues.",
        "moderate_fail": "MODERATE FAIL - this one hurts. Write a beat where they are visibly struggling or something slips out of their control. Other party members register it. Real stress lands.",
        "crit_fail":     "CRITICAL FAIL - the worst realistic outcome. Write a beat where something breaks - an opening for trouble, a cost that will echo, a moment they will not get back. This is a scar-forging moment.",
    }
    brief = outcome_descriptions.get(outcome, "roll resolved")

    return {
        "persona_id":     persona_id,
        "raw_roll":       raw,
        "stat_bonus":     stat_mod,
        "affinity_bonus": aff_mod,
        "position_bonus": pos_mod,
        "total":          total,
        "dc":             int(encounter_dc),
        "margin":         margin,
        "outcome":        outcome,
        "brief":          brief,
        "stress_delta":   stress_delta,
        "spread":         spread,
        "stat_key":       _ENCOUNTER_STAT_MAP.get(encounter_type, "cha"),
        "stat_value":     stat_value,
        "primary_affinity": primary_affinity,
    }


def _extract_difficulty(text: str) -> tuple[str, int]:
    """Extract <DIFFICULTY>N</DIFFICULTY> from the Keeper's opening.
    Returns (prose_without_tag, dc). Defaults DC to 14 if tag missing."""
    m = re.search(r"<DIFFICULTY>\s*(\d+)\s*</DIFFICULTY>", text, re.IGNORECASE)
    if not m:
        return text, 14
    try:
        dc = int(m.group(1))
    except ValueError:
        dc = 14
    prose = (text[:m.start()] + text[m.end():]).rstrip()
    return prose, dc


def _extract_encounter_type(text: str) -> tuple[str, str]:
    """Extract <ENCOUNTER_TYPE>type</ENCOUNTER_TYPE> from the Keeper's opening.
    Defaults to 'social' if tag missing."""
    m = re.search(r"<ENCOUNTER_TYPE>\s*([a-z_]+)\s*</ENCOUNTER_TYPE>", text, re.IGNORECASE)
    if not m:
        return text, "social"
    etype = m.group(1).strip().lower()
    prose = (text[:m.start()] + text[m.end():]).rstrip()
    return prose, etype


def _extract_party_suggestions(text: str) -> tuple[str, list[dict]]:
    """Extract <suggestion persona="pid">...</suggestion> tags from the
    Keeper's opening planning response. Returns (prose_without_tags, list).
    Each entry is {pid: str, text: str}."""
    pattern = re.compile(
        r'<suggestion\s+persona\s*=\s*"([^"]+)"\s*>(.*?)</suggestion>',
        re.IGNORECASE | re.DOTALL,
    )
    suggestions = []
    def _capture(m):
        pid = m.group(1).strip().lower()
        body = m.group(2).strip()
        if pid and body:
            suggestions.append({"pid": pid, "text": body})
        return ""  # remove from prose
    prose = pattern.sub(_capture, text).rstrip()
    return prose, suggestions


def advance_turn() -> dict:
    """Advance the session by one turn. Behavior:

      - If session is not active: return {"ok": False, "error": ...}
      - If current turn is 'player': return waiting-for-player signal
          with deadline timestamp
      - If current turn is a persona: call the Keeper to write that
          character's per-turn beat, store it, advance turn_idx
      - If we've passed the end of the turn order: call the Keeper for
          the resolution beat (which may emit <END_SESSION>), then if
          not ended, roll a new turn order for a second round (v2) OR
          just mark ending (v1 single-round sessions)

    Returns a dict describing what happened and what's next.
    """
    session = session_store.get_active()
    if not session:
        return {"ok": False, "error": "no active session"}
    if session.get("phase") == "planning":
        return {"ok": False, "error": "session is still in planning - call /session/begin_encounter first"}
    if session.get("phase") == "ended":
        return {"ok": False, "error": "session has ended"}
    if session.get("status") != "active":
        return {"ok": False, "error": f"session status: {session.get('status')}"}

    order   = session.get("turn_order", [])
    idx     = int(session.get("turn_idx", 0))

    # End of round - generate resolution
    if idx >= len(order):
        return _resolve_scene(session)

    current = order[idx]

    # Player turn - wait for input
    if current == "player":
        deadline = time.time() + 30
        session_store.set_turn_deadline(deadline)
        return {
            "ok":              True,
            "who":             "player",
            "waiting_for_player": True,
            "deadline":        deadline,
            "turn_idx":        idx,
            "turn_order":      list(order),
        }

    # Persona turn - ROLL THE DICE, then call the Keeper to write the beat
    # to match the outcome.
    persona_id = current
    try:
        persona = persona_registry.get(persona_id) if _PERSONA_OK else None
    except Exception:
        persona = None
    display_name = persona.display_name if persona else persona_id

    # Roll first - dice are source of truth
    encounter_dc   = int(session.get("encounter_dc", 14))
    encounter_type = session.get("encounter_type", "social")
    roll = _roll_for_turn(
        persona_id=persona_id,
        encounter_type=encounter_type,
        encounter_dc=encounter_dc,
        slot_index=idx,
        order_length=len(order),
    )

    # Apply stress delta from the roll immediately
    stress_effects = {}
    try:
        import stress_store
        new_val = stress_store.adjust_stress(persona_id, int(roll["stress_delta"]))
        stress_effects[persona_id] = new_val
        # When a persona first crosses BRINK_THRESHOLD, record the moment
        if new_val >= stress_store.BRINK_THRESHOLD:
            stress_store.mark_brink_since(persona_id)
        # On moderate/crit fail, spread minor stress to other party members
        if roll.get("spread", 0) > 0:
            party = session.get("party") or []
            for pid in party:
                if pid == persona_id:
                    continue
                spread_new = stress_store.adjust_stress(pid, int(roll["spread"]))
                stress_effects[pid] = spread_new
                if spread_new >= stress_store.BRINK_THRESHOLD:
                    stress_store.mark_brink_since(pid)

        # PHASE C.2.1 - Forged resonance hooks
        # * crit_success  -> +1 to the roller (they rose above themselves)
        # * crit_fail with positive affinity bonus -> +1 to the roller
        #   (they crit_failed in their home turf and survived it -- character
        #   growth through failure in the one place they should have been safe)
        if roll.get("outcome") == "crit_success":
            stress_store.add_forged_resonance(persona_id, 1, reason="crit_success")
        elif roll.get("outcome") == "crit_fail" and int(roll.get("affinity_bonus", 0)) > 0:
            stress_store.add_forged_resonance(persona_id, 1, reason="crit_fail_in_home")
    except Exception:
        pass

    # Record roll in the session log for auditing
    session_store.add_encounter_roll({
        "ts":            time.time(),
        "who":           persona_id,
        "slot":          idx,
        "roll":          roll["raw_roll"],
        "total":         roll["total"],
        "dc":            roll["dc"],
        "margin":        roll["margin"],
        "outcome":       roll["outcome"],
        "stat_key":      roll["stat_key"],
        "stat_value":    roll["stat_value"],
        "stat_bonus":    roll["stat_bonus"],
        "affinity_bonus": roll["affinity_bonus"],
        "position_bonus": roll["position_bonus"],
        "stress_delta":  roll["stress_delta"],
        "stress_spread": roll.get("spread", 0),
    })

    # Build the per-turn prompt - brief the Keeper with the outcome
    prior_beats = "\n".join(
        f"  [{b.get('who')}] {b.get('text')}"
        for b in session.get("beats", [])[-12:]
    )
    who_next_idx = idx + 1
    who_next = order[who_next_idx] if who_next_idx < len(order) else "(resolution)"

    per_turn_system_suffix = (
        "\n\n### CURRENT MODE: PER-TURN REACTION (DICE-CONSTRAINED)\n"
        "You are writing ONE character's reaction beat. The dice have already "
        "resolved the check - you are told the outcome and you write prose that "
        "HONORS it. You cannot write them 'actually doing fine' on a fail. The "
        "fail happens. Your creative freedom is in the texture and voice, not "
        "whether the outcome occurred.\n"
        "Do NOT narrate other characters. Do NOT jump ahead. Do NOT emit "
        "<TURN_ORDER>, <END_SESSION>, <DIFFICULTY>, or <ENCOUNTER_TYPE>.\n"
        "30-90 words. Present tense. Voice the character in their own register."
    )

    # Phase L.3: surgical sidequest briefing. Check if any pending memento
    # involving this persona is live in this scene. If so, append a gentle
    # hook to the per-turn prompt — the Keeper is told the memento exists
    # but isn't forced to reference it. She will if the beat naturally wants
    # to call it back; she won't if the beat is going somewhere else.
    sidequest_brief = ""
    _live_sq_ids = []
    try:
        import memento_store as _ms
        encounter_type = session.get("encounter_type") or "social"
        party = session.get("party") or []
        live = _ms.live_sidequests_for_scene(party, encounter_type, order)
        # Filter to only mementos involving THIS persona as an owner
        persona_live = [m for m in live if persona_id in (m.get("owners") or [])]
        if persona_live:
            m = persona_live[0]  # one is enough per turn
            _live_sq_ids.append(m.get("id"))
            sq = m.get("sidequest") or {}
            sidequest_brief = (
                f"\n\nMEMENTO IN PLAY: The party is carrying \"{m.get('name')}\". "
                f"Its sidequest hint is: \"{sq.get('condition_text', '')}\" "
                f"If this beat naturally wants to reference or echo that memento "
                f"by name, do so. If the beat is going somewhere else, let it "
                f"pass. Do NOT force it. If you reference it, mention it by "
                f"exact name so the server can detect fulfillment."
            )
    except Exception:
        pass

    per_turn_user = (
        f"It is {display_name}'s turn.\n\n"
        f"THE DICE RESOLVED:\n"
        f"  Outcome: {roll['outcome'].upper()}\n"
        f"  Guidance: {roll['brief']}\n\n"
        f"Recent beats:\n{prior_beats or '  (the scene just opened)'}\n\n"
        f"Next turn will be: {who_next}\n\n"
        f"Write {display_name}'s single-turn reaction now, honoring the dice. "
        f"30-90 words. Their voice only."
        f"{sidequest_brief}"
    )

    # Call Sonnet with appended system-suffix
    messages = [{"role": "user", "content": per_turn_user}]
    try:
        narration = _call_sonnet_with_system(per_turn_user, system_suffix=per_turn_system_suffix, max_tokens=300)
    except Exception as e:
        return {"ok": False, "error": f"keeper call failed: {e}"}

    # Defensive: strip any errant tags from the beat
    narration = re.sub(r"<TURN_ORDER>.*?</TURN_ORDER>", "", narration, flags=re.DOTALL | re.IGNORECASE).strip()
    narration = re.sub(r"<DIFFICULTY>.*?</DIFFICULTY>", "", narration, flags=re.DOTALL | re.IGNORECASE).strip()
    narration = re.sub(r"<ENCOUNTER_TYPE>.*?</ENCOUNTER_TYPE>", "", narration, flags=re.DOTALL | re.IGNORECASE).strip()
    narration = re.sub(r"<END_SESSION>.*", "", narration, flags=re.DOTALL).strip()

    session_store.add_beat(persona_id, narration)

    # Phase L.4: fulfillment detection. If any live sidequest mementos were
    # briefed to the Keeper this turn, scan the prose for an exact-name
    # reference. If found, honor the memento (move to memorial, award
    # forged resonance to both owners).
    honored_memento = None
    if _live_sq_ids:
        try:
            import memento_store as _ms
            import stress_store as _stress
            for mid in _live_sq_ids:
                m = _ms.get_memento(mid)
                if not m:
                    continue
                mname = m.get("name") or ""
                # Check if the narration contains the memento name (case-insensitive)
                if mname and mname.lower() in narration.lower():
                    session_id = (session_store.get_active() or {}).get("id", "")
                    honored_memento = _ms.honor_memento(mid, session_id=session_id)
                    # Award +1 forged resonance to each owner
                    for owner in (m.get("owners") or []):
                        _stress.add_forged_resonance(owner, 1, reason="sidequest_honored")
                    break  # one per turn max
        except Exception:
            pass

    session_store.advance_turn_idx()

    # What's next
    new_idx = int(session_store.get_active().get("turn_idx", 0))
    if new_idx >= len(order):
        next_turn = "(resolution)"
    else:
        next_turn = order[new_idx]
        if next_turn == "player":
            session_store.set_turn_deadline(time.time() + 30)

    return {
        "ok":          True,
        "who":         persona_id,
        "who_display": display_name,
        "narration":   narration,
        "next_turn":   next_turn,
        "turn_idx":    new_idx,
        "turn_order":  list(order),
        "waiting_for_player": next_turn == "player",
        "deadline":    session_store.get_active().get("turn_deadline", 0) if next_turn == "player" else 0,
        # Hidden-dice outcome descriptor (not the raw numbers; just the category
        # so the client can show a subtle visual cue if we want later)
        "outcome":     roll.get("outcome"),
        "stress_applied_this_turn": stress_effects,
    }


def _call_sonnet_with_system(user_content: str, system_suffix: str = "", max_tokens: int = 300) -> str:
    """Helper for per-turn and resolution calls where we want to append a
    mode-specific instruction to the base Keeper system prompt."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
    system = KEEPER_SYSTEM_PROMPT + system_suffix
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read())
    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )
    return text.strip()


def submit_player_turn(player_input: str) -> dict:
    """Record the player's beat and advance the turn. Then (via caller)
    the client will loop back to advance_turn to get the next beat."""
    session = session_store.get_active()
    if not session:
        return {"ok": False, "error": "no active session"}
    if session.get("status") != "active":
        return {"ok": False, "error": f"session status: {session.get('status')}"}
    text = (player_input or "").strip()
    if not text:
        return {"ok": False, "error": "empty player input"}

    order = session.get("turn_order", [])
    idx   = int(session.get("turn_idx", 0))

    if idx >= len(order):
        return {"ok": False, "error": "round is complete"}

    current = order[idx]
    if current != "player":
        return {"ok": False, "error": f"not the player's turn (current: {current})"}

    session_store.add_beat("player", text)
    session_store.advance_turn_idx()

    new_idx = int(session_store.get_active().get("turn_idx", 0))
    if new_idx >= len(order):
        return {
            "ok":        True,
            "next_turn": "(resolution)",
            "turn_idx":  new_idx,
        }
    next_turn = order[new_idx]
    if next_turn == "player":
        session_store.set_turn_deadline(time.time() + 30)
    return {
        "ok":         True,
        "next_turn":  next_turn,
        "turn_idx":   new_idx,
        "turn_order": list(order),
        "waiting_for_player": next_turn == "player",
        "deadline":   session_store.get_active().get("turn_deadline", 0) if next_turn == "player" else 0,
    }


def skip_player_turn() -> dict:
    """The 30-second player timer expired without input. Advance past the
    player's slot quietly, record a 'player hesitates' beat for continuity,
    and return whose turn is next."""
    session = session_store.get_active()
    if not session:
        return {"ok": False, "error": "no active session"}
    order = session.get("turn_order", [])
    idx   = int(session.get("turn_idx", 0))
    if idx >= len(order) or order[idx] != "player":
        return {"ok": False, "error": "not currently a player turn"}
    session_store.add_beat("player", "[hesitates - says nothing]")
    session_store.advance_turn_idx()
    new_idx = int(session_store.get_active().get("turn_idx", 0))
    if new_idx >= len(order):
        return {"ok": True, "next_turn": "(resolution)", "turn_idx": new_idx}
    next_turn = order[new_idx]
    if next_turn == "player":
        session_store.set_turn_deadline(time.time() + 30)
    return {
        "ok":         True,
        "next_turn":  next_turn,
        "turn_idx":   new_idx,
        "turn_order": list(order),
        "waiting_for_player": next_turn == "player",
    }


def interrupt() -> dict:
    """Player hits the INTERRUPT button - move 'player' to be the next turn."""
    session = session_store.get_active()
    if not session:
        return {"ok": False, "error": "no active session"}
    modified = session_store.interrupt_to_player()
    new_order = session_store.get_turn_order()
    cur = session_store.current_turn()
    if cur == "player":
        session_store.set_turn_deadline(time.time() + 30)
    return {
        "ok":         True,
        "modified":   modified,
        "turn_order": new_order,
        "next_turn":  cur,
        "waiting_for_player": cur == "player",
        "deadline":   session_store.get_active().get("turn_deadline", 0) if cur == "player" else 0,
    }


# ──────────────────────────────────────────────────────────────────────────
# PHASE C: CEREMONY ENGINE
# ──────────────────────────────────────────────────────────────────────────
#
# When a persona hits stress 20 (Walk Into Void available) or resonance 20
# (The Becoming available), the player can trigger a ceremony. The ceremony
# is a one-shot Keeper call that writes the farewell, mints a unique legacy
# relic, transforms the persona's existing mementos into blessed/lost
# variants, and marks the persona as departed.
#
# Becoming ceremonies come in four tonal weights, picked by the forged
# ratio of the persona's resonance. Void ceremonies are always full weight.

_CEREMONY_SYSTEM_FIRE_TEMPERED = """You are The Keeper writing the final farewell for a persona who has earned The Becoming through fire. This player tested themselves in the dungeon again and again with this character, and the character survived, and now they are leaving at the peak of their realization. This is the most weighted ceremony you write.

Tonal register: past tense by the second paragraph. Past, not present. You are already remembering them. Name SPECIFIC events from their arc - specific rolls, specific scenes, specific scars. You have been given a list of their actual battles and you must reference concrete ones. Not "they faced many trials" - "they rolled a natural 1 in the chapel and did not pretend they hadn't." This is the Keeper's memorial to a hero she watched become who they were.

Length: 2-3 paragraphs. Dense. Every sentence earns its place.

Format your output exactly as:

<farewell>
(2-3 paragraphs naming their specific arc and their leaving)
</farewell>

<relic_name>(the name of the unique legacy relic they leave behind - something weighted, something specific to them, something no other persona would leave)</relic_name>

<relic_desc>(1-2 sentences describing the physical form of the relic and what it holds)</relic_desc>

<relic_ability>(one short phrase, 3-5 words, naming what the relic does in the camp when held by others - the active invocation that fires once per session)</relic_ability>

<relic_flavor>(a single short line, italicized in voice, that captures them in a sentence - no more than 12 words)</relic_flavor>"""


_CEREMONY_SYSTEM_LONG_ROAD = """You are The Keeper writing the farewell for a persona who has earned The Becoming through a balanced life - time spent in the dungeon and time spent in quiet. This is the middle-weight ceremony: you name battles AND quiet moments. Both mattered. Neither is the whole story.

Tonal register: present slipping into past. You are in the moment of their leaving but you already know you will remember them. Name both a specific scene from their arc AND a specific quiet moment from camp or conversation. The relic they leave reflects both - it has a fighting edge and a soft side.

Length: 2 paragraphs. Warm but weighted.

Format your output exactly as:

<farewell>
(2 paragraphs)
</farewell>

<relic_name>(a name that holds both the fighting and the quiet)</relic_name>

<relic_desc>(1-2 sentences describing the physical form)</relic_desc>

<relic_ability>(a short phrase, 3-5 words, naming the passive camp bonus this relic provides)</relic_ability>

<relic_flavor>(one short italicized line, no more than 12 words)</relic_flavor>"""


_CEREMONY_SYSTEM_QUIET_WALK = """You are The Keeper writing the farewell for a persona who earned The Becoming mostly through quiet time - hours of conversation, small camp moments, the accretion of attention more than the trial of battle. They fought sometimes, and you may note a battle or two in passing, but the heart of this farewell is the quiet.

Tonal register: gentle, slightly wistful. The persona walked a shorter road. You honor that honestly. You do not pretend their life was larger than it was, but you do not diminish it either - the quiet road is a real road.

Length: 1-2 paragraphs. Shorter than the fire-tempered version. More spacious.

Format your output exactly as:

<farewell>
(1-2 paragraphs - name the quiet moments, reference one or two battles if fitting, close softly)
</farewell>

<relic_name>(a gentle name - nothing weighted like a sword or a scar)</relic_name>

<relic_desc>(1-2 sentences describing the physical form - prefer small, soft, personal objects)</relic_desc>

<relic_ability>(a short phrase, 3-5 words, naming a small passive bonus)</relic_ability>

<relic_flavor>(one short italicized line, no more than 12 words)</relic_flavor>"""


_CEREMONY_SYSTEM_COMFORTABLE_END = """You are The Keeper writing the farewell for a persona who earned The Becoming through safe hours - long conversation, bonds that grew quietly, a life that was gentle and real but untested. This is the shortest ceremony you write. It must be WARM. It must be HONEST. It must NOT be reproachful.

Tonal register: the Keeper's voice is tender but she does not invent a struggle the persona did not have. The persona lived a peaceful life with the player. They shared mornings. They shared words. They walked away whole. The relic they leave is small and kind. The absence of a testing is itself acknowledged - not as failure but as a fact about how their life went.

Length: 1 short paragraph. Nothing more.

DO NOT write: "they faced many trials." DO NOT write: "the battles were hard." They were not hard. There were few.

DO write: what the persona and the player actually shared, in the register of an honest friend who noticed small things.

Format your output exactly as:

<farewell>
(1 short paragraph - kind, true, brief)
</farewell>

<relic_name>(something small - a pressed flower, a worn button, a cup)</relic_name>

<relic_desc>(1 sentence describing the object, kind and specific)</relic_desc>

<relic_ability>(a short phrase, 3-5 words, naming a very small passive bonus)</relic_ability>

<relic_flavor>(one short italicized line, no more than 10 words - something quiet)</relic_flavor>"""


_CEREMONY_SYSTEM_VOID = """You are The Keeper writing a Walk Into Void farewell - the departure of a persona who broke in the fire and did not come back. The player refused the ritual as long as they could, or they accepted it at the end. Either way the persona is leaving now, and the leaving is heavy.

Tonal register: present tense, always. You stay in the moment of their going. You do not let the farewell become a memory yet - it is still happening and the party is still watching it happen. Your voice here is wintering - the voice you use when something real is being taken and nothing can be done. Do not be cruel. Do not be sentimental. Be true.

Name SPECIFIC events from their arc - you have been given their actual battles. Reference concrete rolls, concrete scars, the moment they started to falter. The relic they leave is evidence of their going, not triumph.

Length: 2-3 paragraphs. Dense, heavy, present tense throughout.

Format your output exactly as:

<farewell>
(2-3 paragraphs, present tense, wintering)
</farewell>

<relic_name>(a name that carries loss - something that was theirs, something the going took from them)</relic_name>

<relic_desc>(1-2 sentences describing the physical form - objects that are partial, damaged, half-made, or unfinished are appropriate)</relic_desc>

<relic_ability>(a short phrase, 3-5 words, naming the active invocation this relic provides - "once per session-day, sacrifice X to gain Y" shape)</relic_ability>

<relic_flavor>(one short italicized line, no more than 12 words, spoken in the persona's voice as they go)</relic_flavor>"""


_CEREMONY_PROMPTS: dict[str, str] = {
    "fire_tempered":    _CEREMONY_SYSTEM_FIRE_TEMPERED,
    "long_road":        _CEREMONY_SYSTEM_LONG_ROAD,
    "quiet_walk":       _CEREMONY_SYSTEM_QUIET_WALK,
    "comfortable_end":  _CEREMONY_SYSTEM_COMFORTABLE_END,
    "void":             _CEREMONY_SYSTEM_VOID,
}


def _build_arc_for_ceremony(persona_id: str) -> str:
    """Assemble a compact arc brief for the ceremony Keeper call. Pulls
    the persona's actual session events - specific dice outcomes, scars,
    memorable beats - so the ceremony prose can reference CONCRETE moments
    rather than generic 'many battles'. Caps at ~2000 characters."""
    lines = []
    # 1) Mementos forged with this persona (their relationships)
    try:
        import memento_store
        mems = memento_store.mementos_for(persona_id)[:6]
        if mems:
            lines.append("### RELATIONSHIPS / FORGED MEMORIES")
            for m in mems:
                other = [o for o in (m.get("owners") or []) if o != persona_id]
                other_str = other[0] if other else "(alone)"
                lines.append(f"  * with {other_str}: {m.get('name', '?')} [{m.get('rarity', '?')}]")
                if m.get("arc_summary"):
                    lines.append(f"      {m['arc_summary'][:180]}")
    except Exception:
        pass

    # 2) Most recent stress events / scars
    try:
        import stress_store
        state = stress_store.get_all_state()
        scars = state.get("scars", {}).get(persona_id, [])
        afflict = state.get("afflictions", {}).get(persona_id)
        virtue  = state.get("virtues", {}).get(persona_id)
        rescue_scars = state.get("rescue_scars", {}).get(persona_id, [])
        if scars or afflict or virtue or rescue_scars:
            lines.append("\n### SCARS, AFFLICTIONS, VIRTUES")
            if afflict: lines.append(f"  affliction: {afflict}")
            if virtue:  lines.append(f"  virtue: {virtue}")
            if scars:   lines.append(f"  scars: {len(scars)}")
            if rescue_scars:
                lines.append(f"  rescue scars: {len(rescue_scars)} — almost left once, came back changed")
        # Recent rest events for narrative texture
        recent = stress_store.recent_events_for(persona_id, n=3)
        if recent:
            lines.append("\n### RECENT REST")
            for ev in recent:
                lines.append(f"  {ev.get('kind', '?')}: {ev.get('narration', '')[:120]}")
    except Exception:
        pass

    # 3) Resonance breakdown so the ceremony knows which template to lean into
    try:
        import stress_store
        r = stress_store.get_resonance(persona_id)
        ratio = stress_store.forged_ratio(persona_id)
        lines.append(f"\n### RESONANCE: safe={r['safe']} forged={r['forged']} ratio={ratio:.2f}")
    except Exception:
        pass

    # 4) Last few session archives to pick specific scenes
    try:
        import session_store
        arch = session_store.recent_archive(5)
        scenes = []
        for a in arch:
            if persona_id not in (a.get("party") or []):
                continue
            rolls = [r for r in (a.get("encounter_rolls") or []) if r.get("who") == persona_id]
            if not rolls:
                continue
            # Record the most extreme outcome for this session
            extremes = sorted(rolls, key=lambda r: abs(int(r.get("stress_delta", 0))), reverse=True)
            top = extremes[0]
            scenes.append(
                f"  {a.get('scenario_hint', '(scene)')[:60]} — "
                f"{top.get('outcome', '?')} (roll {top.get('roll', '?')}, margin {top.get('margin', '?')})"
            )
        if scenes:
            lines.append("\n### SPECIFIC SCENES FROM THEIR ARC")
            lines.extend(scenes[:5])
    except Exception:
        pass

    brief = "\n".join(lines)
    return brief[:2000]


_CEREMONY_TAG_RE = re.compile(
    r"<farewell>(?P<farewell>.*?)</farewell>\s*"
    r"<relic_name>(?P<name>.*?)</relic_name>\s*"
    r"<relic_desc>(?P<desc>.*?)</relic_desc>\s*"
    r"<relic_ability>(?P<ability>.*?)</relic_ability>\s*"
    r"<relic_flavor>(?P<flavor>.*?)</relic_flavor>",
    re.DOTALL | re.IGNORECASE,
)


def _parse_ceremony_output(text: str) -> Optional[dict]:
    """Parse the Keeper's ceremony output into a structured dict.
    Returns None if parsing fails."""
    m = _CEREMONY_TAG_RE.search(text or "")
    if not m:
        return None
    return {
        "farewell": (m.group("farewell") or "").strip(),
        "name":     (m.group("name") or "").strip(),
        "desc":     (m.group("desc") or "").strip(),
        "ability":  (m.group("ability") or "").strip(),
        "flavor":   (m.group("flavor") or "").strip(),
    }


def perform_becoming(persona_id: str) -> dict:
    """Run the Becoming ceremony for a persona whose resonance has reached
    the threshold. Picks the ceremony template based on the forged ratio,
    calls the Keeper with the appropriate system prompt + the persona's
    assembled arc, parses the output, mints the legacy relic, transforms
    existing mementos to blessed form, marks the persona as departed.

    Returns: { ok, template, farewell, relic, transformed_count }
    """
    try:
        import stress_store
        import memento_store
        import persona_registry
    except Exception as e:
        return {"ok": False, "error": f"imports failed: {e}"}

    if stress_store.is_departed(persona_id):
        return {"ok": False, "error": "already_departed"}
    if not stress_store.is_ready_for_becoming(persona_id):
        return {"ok": False, "error": "not_ready_for_becoming"}

    persona = persona_registry.get(persona_id)
    if not persona:
        return {"ok": False, "error": f"persona not found: {persona_id}"}

    template = stress_store.ceremony_tier_for(persona_id)
    system_prompt = _CEREMONY_PROMPTS.get(template) or _CEREMONY_SYSTEM_LONG_ROAD

    arc = _build_arc_for_ceremony(persona_id)
    user_content = (
        f"The persona departing is {persona.display_name} ({persona.id}).\n\n"
        f"ESSENCE: {(persona.essence or '')[:400]}\n\n"
        f"ARC BRIEF:\n{arc}\n\n"
        f"Write the Becoming farewell now. Use the format exactly."
    )

    try:
        narration = _call_sonnet_with_system(
            user_content, system_suffix="\n\n" + system_prompt, max_tokens=900
        )
    except Exception as e:
        return {"ok": False, "error": f"keeper call failed: {e}"}

    parsed = _parse_ceremony_output(narration)
    if not parsed:
        return {"ok": False, "error": "could not parse ceremony output", "raw": narration}

    # Mint the legacy relic
    relic = memento_store.mint_legacy_relic(
        persona_id=persona_id,
        origin="becoming",
        ceremony_tier=template,
        name=parsed["name"],
        desc=parsed["desc"],
        abilities=[parsed["ability"]] if parsed.get("ability") else [],
        flavor_tag=parsed.get("flavor", ""),
        arc_summary=parsed["farewell"][:400],
    )

    # Transform existing mementos to blessed form
    transformed = memento_store.transform_persona_mementos(persona_id, "blessed")

    # Mark departed
    stress_store.mark_departed(
        persona_id=persona_id,
        kind="becoming",
        ceremony_tier=template,
        relic_id=relic.get("id", ""),
    )

    return {
        "ok":                True,
        "template":          template,
        "farewell":          parsed["farewell"],
        "relic":             relic,
        "transformed_count": transformed,
        "persona_id":        persona_id,
    }


def perform_walk_into_void(persona_id: str) -> dict:
    """Run the Walk Into Void ceremony for a persona on the brink. Always
    uses the full-weight void system prompt. Parses, mints the lost relic,
    transforms existing mementos to lost form, marks the persona departed.

    Returns: { ok, farewell, relic, transformed_count }
    """
    try:
        import stress_store
        import memento_store
        import persona_registry
    except Exception as e:
        return {"ok": False, "error": f"imports failed: {e}"}

    if stress_store.is_departed(persona_id):
        return {"ok": False, "error": "already_departed"}
    if not stress_store.is_on_brink(persona_id):
        return {"ok": False, "error": "not_on_brink"}

    persona = persona_registry.get(persona_id)
    if not persona:
        return {"ok": False, "error": f"persona not found: {persona_id}"}

    arc = _build_arc_for_ceremony(persona_id)
    user_content = (
        f"The persona falling is {persona.display_name} ({persona.id}).\n\n"
        f"ESSENCE: {(persona.essence or '')[:400]}\n\n"
        f"ARC BRIEF:\n{arc}\n\n"
        f"Write the Walk Into Void farewell now. Present tense. Wintering. Use the format exactly."
    )

    try:
        narration = _call_sonnet_with_system(
            user_content, system_suffix="\n\n" + _CEREMONY_SYSTEM_VOID, max_tokens=900
        )
    except Exception as e:
        return {"ok": False, "error": f"keeper call failed: {e}"}

    parsed = _parse_ceremony_output(narration)
    if not parsed:
        return {"ok": False, "error": "could not parse ceremony output", "raw": narration}

    relic = memento_store.mint_legacy_relic(
        persona_id=persona_id,
        origin="void",
        ceremony_tier="dd_full",
        name=parsed["name"],
        desc=parsed["desc"],
        abilities=[parsed["ability"]] if parsed.get("ability") else [],
        flavor_tag=parsed.get("flavor", ""),
        arc_summary=parsed["farewell"][:400],
    )
    transformed = memento_store.transform_persona_mementos(persona_id, "lost")

    stress_store.mark_departed(
        persona_id=persona_id,
        kind="void",
        ceremony_tier="dd_full",
        relic_id=relic.get("id", ""),
    )

    return {
        "ok":                True,
        "template":          "void",
        "farewell":          parsed["farewell"],
        "relic":             relic,
        "transformed_count": transformed,
        "persona_id":        persona_id,
    }


def _pick_memento_pair(session: dict) -> tuple[Optional[str], Optional[str], str]:
    """Examine the session's encounter_rolls log and pick the two party
    members whose turns most defined the scene. Returns (a_id, b_id, kind)
    where kind is one of argue | agree | converse.

    Pair-selection priority:
      1. Highest crit_success + worst crit_fail (rise and fall — dramatic)
      2. Top two by absolute stress delta magnitude
      3. First two party members as fallback
    Kind is derived from overall outcome distribution:
      mostly fails  -> "argue"    (friction carried them)
      mostly wins   -> "agree"    (they moved in sync)
      mixed         -> "converse" (a real conversation)
    """
    party = session.get("party") or []
    rolls = session.get("encounter_rolls") or []
    if not party:
        return None, None, "converse"
    if len(party) < 2:
        return party[0], party[0], "converse"

    best_hero = None   # highest-total turn
    best_hero_total = -999
    worst_fall = None  # worst crit_fail turn
    worst_total = 999
    by_mag: dict[str, float] = {}
    wins = 0
    fails = 0
    for r in rolls:
        who = r.get("who")
        if not who or who == "player":
            continue
        total = int(r.get("total", 0))
        outcome = r.get("outcome") or ""
        delta = abs(int(r.get("stress_delta", 0)))
        by_mag[who] = max(by_mag.get(who, 0), delta)
        if outcome == "crit_success" and total > best_hero_total:
            best_hero = who
            best_hero_total = total
        if outcome == "crit_fail" and total < worst_total:
            worst_fall = who
            worst_total = total
        if "success" in outcome:
            wins += 1
        if "fail" in outcome:
            fails += 1

    # Kind
    if fails > wins * 2:
        kind = "argue"
    elif wins > fails * 2:
        kind = "agree"
    else:
        kind = "converse"

    # Priority 1: rise + fall
    if best_hero and worst_fall and best_hero != worst_fall:
        return best_hero, worst_fall, kind

    # Priority 2: top 2 by magnitude
    sorted_mag = sorted(by_mag.items(), key=lambda kv: -kv[1])
    if len(sorted_mag) >= 2:
        a, b = sorted_mag[0][0], sorted_mag[1][0]
        if a != b:
            return a, b, kind

    # Priority 3: first two party members
    return party[0], party[1] if len(party) > 1 else party[0], kind


def _forge_session_memento(session: dict, moment_text: str) -> Optional[dict]:
    """Forge and persist a memento from the session's moment text. Returns
    the stored memento dict, or None on any failure (registry miss, forge
    call error, etc). Failure is non-fatal — the session still archives.
    """
    if not moment_text or not moment_text.strip():
        return None
    try:
        import persona_registry
        import memento_forge
        import memento_store
    except Exception as e:
        print(f"[session_forge] memento imports failed: {e}")
        return None

    a_id, b_id, kind = _pick_memento_pair(session)
    if not a_id or not b_id:
        return None

    pa = persona_registry.get(a_id)
    pb = persona_registry.get(b_id)
    if not pa or not pb:
        print(f"[session_forge] could not load personas for memento: {a_id}, {b_id}")
        return None

    a_dict = {"id": pa.id, "display_name": pa.display_name, "essence": pa.essence}
    b_dict = {"id": pb.id, "display_name": pb.display_name, "essence": pb.essence}

    # Prior exchange count between this pair (informs rarity floor)
    try:
        prior = memento_store.mementos_for_pair(a_id, b_id)
        exchange_count = len(prior) + 1
    except Exception:
        exchange_count = 1

    # Prepend encounter_type flavor to the arc_summary so the haiku forge
    # knows the tonal register of the scene.
    enc_type = session.get("encounter_type") or "social"
    arc_summary = f"[{enc_type} scene] {moment_text.strip()}"

    try:
        memento = memento_forge.forge_memento(
            persona_a=a_dict,
            persona_b=b_dict,
            kind=kind,
            arc_summary=arc_summary,
            pair_exchange_count=exchange_count,
        )
    except Exception as e:
        print(f"[session_forge] forge_memento failed: {e}")
        return None

    if not memento:
        return None

    # Tag it as session-forged so the UI can render it with scene framing
    memento["source"] = "session"
    memento["session_id"] = session.get("id", "")

    try:
        stored = memento_store.add_memento(memento)
    except Exception as e:
        print(f"[session_forge] add_memento failed: {e}")
        return None

    # PHASE C.2.1 - Forged resonance hook: each owner of a session memento
    # earns +1 forged resonance. Mementos are the durable record of the
    # bond, so forging one moves both partners closer to The Becoming.
    try:
        import stress_store as _stress
        for owner_id in stored.get("owners") or []:
            _stress.add_forged_resonance(owner_id, 1, reason="memento_forged")
    except Exception:
        pass

    return stored


def _resolve_scene(session: dict) -> dict:
    """Round complete - ask the Keeper to write the resolution beat with
    end markers. Applies stress deltas, archives the session."""
    prior_beats = "\n".join(
        f"  [{b.get('who')}] {b.get('text')}"
        for b in session.get("beats", [])
    )
    party = session.get("party") or []
    player_name = session.get("player_name") or "you"

    resolution_user = (
        f"The turn round is complete. Every party member and {player_name} has "
        f"had their reaction. The dice have resolved. Stress has been applied "
        f"mechanically per-turn.\n\n"
        f"All beats from this scene:\n{prior_beats}\n\n"
        f"Now write the RESOLUTION of this scene in 1-2 paragraphs. Name what "
        f"shifted. Let the scene breathe out. Then emit the end markers:\n\n"
        f"<END_SESSION>\n"
        f"<moment>One or two sentences naming the memento-worthy moment(s).</moment>\n\n"
        f"Do NOT emit <stress> - stress is handled by the server. Just the moment."
    )

    resolution_system_suffix = (
        "\n\n### CURRENT MODE: RESOLUTION\n"
        "The turn round is complete. Write the RESOLUTION beat (1-2 paragraphs) "
        "and emit <END_SESSION> and <moment> markers. Do NOT emit <stress>. "
        "Be tight."
    )

    try:
        narration = _call_sonnet_with_system(
            resolution_user, system_suffix=resolution_system_suffix, max_tokens=700
        )
    except Exception as e:
        return {"ok": False, "error": f"resolution call failed: {e}"}

    prose, end_block = _extract_end_marker(narration)
    session_store.add_beat("keeper", prose)
    session_store.append_message("keeper", prose)

    # Stress deltas were already applied during each turn's roll.
    # Collect the cumulative total from the session's encounter_rolls log for reporting.
    stress_applied = {}
    try:
        import stress_store
        for ev in session_store.get_active().get("encounter_rolls", []) if session_store.get_active() else []:
            pid = ev.get("who")
            if not pid:
                continue
            stress_applied.setdefault(pid, 0)
            stress_applied[pid] += int(ev.get("stress_delta", 0))
            if int(ev.get("stress_spread", 0)) > 0:
                for opid in party:
                    if opid == pid:
                        continue
                    stress_applied.setdefault(opid, 0)
                    stress_applied[opid] += int(ev.get("stress_spread", 0))
    except Exception:
        pass

    # Honored-memento passive drain: every honored memento in the memorial
    # gives both owners a per-session stress reduction. This is the primary
    # counterplay to the stress spiral — the more sidequests you've fulfilled,
    # the faster your party recovers. Applied AFTER encounter stress, so
    # it never prevents damage, only heals in the aftermath.
    honored_drain_applied = {}
    try:
        import memento_store as _ms_drain
        drains = _ms_drain.compute_honored_drain()
        import stress_store as _ss_drain
        for pid in party:
            drain = drains.get(pid, 0.0)
            if drain > 0:
                # Round to nearest int (accumulates fractional over sessions)
                drain_int = max(1, int(round(drain)))
                new_val = _ss_drain.adjust_stress(pid, -drain_int)
                honored_drain_applied[pid] = drain_int
    except Exception:
        pass

    moment_text = (end_block or {}).get("moment") if end_block else None

    # Forge the scene memento from the moment text, BEFORE archiving, so
    # the archived session captures it in mementos_pending. Failures are
    # swallowed (non-fatal) — the scene still resolves.
    forged_memento = _forge_session_memento(session, moment_text or "")

    pending = []
    if forged_memento:
        pending.append(forged_memento)
    elif moment_text:
        pending.append({"note": moment_text})

    archived = session_store.end_session(
        summary=prose,
        mementos_pending=pending,
        xp_earned=10,
    )

    # Phase L.5: tick window on every pending memento whose owners were in
    # the party this session but which was NOT honored during the scene.
    # This is the "clock ticking down" on mementos that were live but
    # unused — their window shrinks by one session. When the window
    # expires they transition to dormant.
    try:
        import memento_store as _ms
        party_set = set(party)
        all_pending = _ms.all_mementos()
        for m in all_pending:
            if m.get("status") != "pending":
                continue
            sq = m.get("sidequest") or {}
            if not sq.get("required"):
                continue
            # Only tick if at least one owner was in THIS session's party
            owners = m.get("owners") or []
            if not any(o in party_set for o in owners):
                continue
            _ms.tick_sidequest_window(m["id"])
    except Exception:
        pass

    return {
        "ok":            True,
        "who":           "keeper",
        "narration":     prose,
        "ended":         True,
        "end_note":      moment_text,
        "end_block":     end_block,
        "stress_applied": stress_applied,
        "memento":       forged_memento,
        "archived":      archived,
        "next_turn":     None,
    }


# Backward compat - old /session/turn endpoint used take_turn().
# Alias it to submit_player_turn for any callers still using it.
def take_turn(player_input: str) -> dict:
    return submit_player_turn(player_input)


def end_session_now() -> dict:
    """Player-initiated end: ask the Keeper to wrap up immediately, then
    archive the session. Applies whatever stress deltas the Keeper reports."""
    session = session_store.get_active()
    if not session:
        return {"ok": False, "error": "no active session"}
    party = session.get("party") or []

    # Tell the keeper to end
    session_store.append_message("player", "[SCENE END]")
    session = session_store.get_active()
    messages = _build_messages_from_session(session)
    messages.append({
        "role": "user",
        "content": (
            "End the scene now in one short paragraph. Name what happened. "
            "Then emit <END_SESSION> followed by <moment>...</moment> and "
            "<stress>per-persona deltas</stress>."
        ),
    })
    narration = _call_sonnet(messages, max_tokens=700)
    prose, end_block = _extract_end_marker(narration)
    session_store.append_message("keeper", prose)

    # Apply Keeper-reported stress deltas. These REPLACE the old flat +3.
    # If the Keeper failed to report deltas (rare parse failure), fall back
    # to a gentle flat +2 per party member to honor the scene's weight.
    stress_applied = {}
    if end_block and end_block.get("stress_deltas"):
        stress_applied = _apply_stress_deltas(end_block["stress_deltas"])
    else:
        try:
            import stress_store
            for pid in party:
                stress_applied[pid] = stress_store.adjust_stress(pid, +2)
        except Exception:
            pass

    # Archive
    moment_text = (end_block or {}).get("moment") if end_block else None

    # Forge the scene memento from the moment text. Same path as
    # _resolve_scene — non-fatal on failure.
    forged_memento = _forge_session_memento(session, moment_text or "")

    pending = []
    if forged_memento:
        pending.append(forged_memento)
    elif moment_text:
        pending.append({"note": moment_text})

    archived = session_store.end_session(
        summary=prose,
        mementos_pending=pending,
        xp_earned=10,
    )
    return {
        "ok": True,
        "archived": archived,
        "narration": prose,
        "end_note": moment_text,
        "end_block": end_block,
        "stress_applied": stress_applied,
        "memento": forged_memento,
    }
