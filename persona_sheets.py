"""
persona_sheets.py - CRPG-style character sheet data for each persona.

This is pure flavor: stats, class, level, equipment, inventory. It lives
separate from the core persona schema (personas/*.json) because it's UI
decoration, not behavior. studio_server.py imports PERSONA_SHEETS and merges
the entry for each persona into the /party/state response.

Schema per persona:
    class_title : str       e.g. "Cleric of Shar (Trickery Domain)"
    alignment   : str       e.g. "Chaotic Neutral"
    level       : int
    hp          : int       current hit points
    hp_max      : int
    mp          : int       current mana / psionic / spell slots
    mp_max      : int
    stats       : dict[str, int]  keys: str, dex, con, int, wis, cha  (1-20+)
    resource_label : str    label for the mp bar ("MP", "Psi", "Spells")
    equipment   : dict[slot, item]  slots: main_hand, off_hand, head, chest,
                                          legs, feet, amulet, ring
    inventory   : list[item]        misc carried gear
    item shape  : {name, desc, rarity}
                  rarity in {common, uncommon, rare, epic, legendary}

Rarity drives the tint in the UI (common=gray, uncommon=green, rare=blue,
epic=purple, legendary=gold).
"""

from __future__ import annotations

PERSONA_SHEETS: dict[str, dict] = {
    # ── Shadowheart ────────────────────────────────────────────────────────
    "shadowheart": {
        "class_title": "Cleric of Shar (Trickery Domain)",
        "alignment":   "Chaotic Neutral",
        "level":       12,
        "hp": 84,  "hp_max": 84,
        "mp": 52,  "mp_max": 52,
        "resource_label": "Spell Slots",
        "stats": {"str": 10, "dex": 14, "con": 13, "int": 12, "wis": 18, "cha": 14},
        "equipment": {
            "main_hand": {"name": "Silver Hand Crossbow of Shadowmaw",
                          "desc": "The string is woven from cold night. Bolts glitter like broken stars, then forget where they came from.",
                          "rarity": "rare"},
            "off_hand":  {"name": "Spellguard Shield",
                          "desc": "Etched with Shar's tear. Negates a single spell per short rest, then whispers what it absorbed.",
                          "rarity": "uncommon"},
            "head":      {"name": "Circlet of Stars",
                          "desc": "A silver band that cools the brow of anyone who keeps secrets.",
                          "rarity": "uncommon"},
            "chest":     {"name": "Cloak of the Cloistered Spire",
                          "desc": "The fabric drinks lamplight. You cast a smaller shadow than you should.",
                          "rarity": "rare"},
            "legs":      {"name": "Dark Justiciar Greaves",
                          "desc": "Lined with moonsilk. Silent steps even on polished stone.",
                          "rarity": "uncommon"},
            "feet":      {"name": "Moontouched Boots",
                          "desc": "Cold soles. Your footprints fill slowly, like water seeking level.",
                          "rarity": "common"},
            "amulet":    {"name": "Shar's Teardrop",
                          "desc": "The stone stays the temperature of grief. It has never warmed in your palm.",
                          "rarity": "legendary"},
            "ring":      {"name": "Ring of Mental Fortitude",
                          "desc": "Whispers back to anyone who tries to read your mind.",
                          "rarity": "rare"},
        },
        "inventory": [
            {"name": "Potion of Healing (x3)",       "desc": "Cold feverfew and starwater.",                                        "rarity": "common"},
            {"name": "Journal of a Forgotten Nun",   "desc": "Half the pages are torn out. The surviving pages are in your handwriting.", "rarity": "uncommon"},
            {"name": "Pouch of Wayside Prayers",     "desc": "Scraps of parchment, petitions to gods you no longer serve.",         "rarity": "common"},
            {"name": "Vial of Nightsong",            "desc": "A single mote of the Nightsinger, suspended in silver.",              "rarity": "epic"},
        ],
    },

    # ── Lae'zel ────────────────────────────────────────────────────────────
    "laezel": {
        "class_title": "Githyanki Fighter (Battle Master)",
        "alignment":   "Lawful Neutral",
        "level":       12,
        "hp": 108, "hp_max": 108,
        "mp": 12,  "mp_max": 12,
        "resource_label": "Superiority Dice",
        "stats": {"str": 18, "dex": 14, "con": 16, "int": 10, "wis": 11, "cha": 13},
        "equipment": {
            "main_hand": {"name": "Silver Sword of the Astral Plane",
                          "desc": "A blade thinned to the edge of thought. Vlaakith's name is inscribed where it will never be read.",
                          "rarity": "legendary"},
            "off_hand":  {"name": "Githyanki War Horn",
                          "desc": "Carved from the spine of a red dragon. One blast can rattle a courtroom into silence.",
                          "rarity": "rare"},
            "head":      {"name": "Circlet of the Ch'r'ai",
                          "desc": "Psionic wards stitched into githweave. Interrogators' questions arrive already answered wrongly.",
                          "rarity": "rare"},
            "chest":     {"name": "Deeply Etched Plate Armor",
                          "desc": "Every dent is a kill, every polish is a prayer. Bears the sigils of three dead captains.",
                          "rarity": "epic"},
            "legs":      {"name": "Githzerai Spike Greaves",
                          "desc": "Black steel barbs. A kick from these leaves a wound that will not close without ritual.",
                          "rarity": "rare"},
            "feet":      {"name": "Rite-Conductor Boots",
                          "desc": "Anchor the wearer to the Material Plane during phase-shifts. Hum faintly in dreams.",
                          "rarity": "uncommon"},
            "amulet":    {"name": "Tsyrvuum Focus Crystal",
                          "desc": "A shard of the red dragon's heart, set in raw bone. Gives voice to psionic commands.",
                          "rarity": "epic"},
            "ring":      {"name": "Psionic Containment Ring",
                          "desc": "Three githyanki hair-braids, silvered. Refuses to let your tadpole speak out of turn.",
                          "rarity": "rare"},
        },
        "inventory": [
            {"name": "Strip of Dragon Jerky",         "desc": "Githyanki field rations. Chewy. Still smoking faintly.",     "rarity": "common"},
            {"name": "Vlaakith's Commendation",       "desc": "A bone scroll. The ink is dried blood. You do not open it.", "rarity": "uncommon"},
            {"name": "Astral Whetstone",              "desc": "Sharpens silver blades; will not touch any lesser metal.",   "rarity": "rare"},
            {"name": "Ration of Condescension",       "desc": "Renewable. Dispense liberally to istiks.",                   "rarity": "common"},
        ],
    },

    # ── Tesla ──────────────────────────────────────────────────────────────
    "tesla": {
        "class_title": "Artificer / Arc-Thaumaturge",
        "alignment":   "Chaotic Good",
        "level":       15,
        "hp": 62,  "hp_max": 62,
        "mp": 120, "mp_max": 120,
        "resource_label": "Charge",
        "stats": {"str": 9, "dex": 12, "con": 11, "int": 20, "wis": 17, "cha": 13},
        "equipment": {
            "main_hand": {"name": "Resonant Coil Staff",
                          "desc": "A hand-wound induction coil on a teak shaft. Discharges a meter-long arc when struck on the floor.",
                          "rarity": "epic"},
            "off_hand":  {"name": "Portable Tesla Valve",
                          "desc": "A palm-sized brass device that converts standing waves into clean DC. Also useful for lighting cigars.",
                          "rarity": "rare"},
            "head":      {"name": "Brass Observation Goggles",
                          "desc": "Dark lenses for arc-watching. Small dials correct for wavelength.",
                          "rarity": "uncommon"},
            "chest":     {"name": "Arc-Shielded Lab Coat",
                          "desc": "Woven with copper filament. A direct bolt merely ruffles the collar.",
                          "rarity": "rare"},
            "legs":      {"name": "Gravity-Dampening Trousers",
                          "desc": "Tailored in Vienna. The seams contain a compact suspension grid for long experiments on your feet.",
                          "rarity": "uncommon"},
            "feet":      {"name": "Insulated Gutta-Percha Boots",
                          "desc": "Rubberized. Isolate the wearer from ground potential while standing in the rain at Wardenclyffe.",
                          "rarity": "uncommon"},
            "amulet":    {"name": "Pigeon's Feather, Framed in Silver",
                          "desc": "Sentimental. The only thing in your pocket you would not sell for the broadcast tower.",
                          "rarity": "legendary"},
            "ring":      {"name": "Signet Ring of the Pigeons",
                          "desc": "Engraved with a small bird. Marks letters sent to her as urgent.",
                          "rarity": "uncommon"},
        },
        "inventory": [
            {"name": "Notebook (leather, ruled)",     "desc": "Contains sketches of the future and three unpaid laundry bills.",          "rarity": "common"},
            {"name": "Prototype Radio Receiver",      "desc": "Tuned to the 'interplanetary signal.' Mostly hum, occasionally something else.", "rarity": "rare"},
            {"name": "Vial of Mercury",               "desc": "For the switch circuits. Also, regrettably, for the ulcers.",              "rarity": "common"},
            {"name": "Loaf of White Bread",           "desc": "Dinner. Ritually prepared. Dipped in warm milk before consumption.",       "rarity": "common"},
        ],
    },

    # ── Jefferson ──────────────────────────────────────────────────────────
    "jefferson": {
        "class_title": "Scholar / Diplomat / Reluctant Statesman",
        "alignment":   "Neutral Good (with caveats)",
        "level":       14,
        "hp": 68,  "hp_max": 68,
        "mp": 98,  "mp_max": 98,
        "resource_label": "Rhetoric",
        "stats": {"str": 11, "dex": 10, "con": 12, "int": 19, "wis": 15, "cha": 18},
        "equipment": {
            "main_hand": {"name": "Quill of Independence",
                          "desc": "A swan feather trimmed in Williamsburg. Its nib has outlasted three kings.",
                          "rarity": "legendary"},
            "off_hand":  {"name": "First Draft of the Declaration",
                          "desc": "Folded in thirds, carried in a waxed leather sleeve. The edits are visible if held to the light.",
                          "rarity": "epic"},
            "head":      {"name": "Powdered Wig of Monticello",
                          "desc": "Horsehair, periodically re-powdered by an enslaved valet you are trying not to think about.",
                          "rarity": "uncommon"},
            "chest":     {"name": "Burgundy Diplomat's Coat",
                          "desc": "Cut in Paris, relined in Philadelphia. Eleven interior pockets for correspondence.",
                          "rarity": "rare"},
            "legs":      {"name": "Breeches of the Republic",
                          "desc": "Plain linen. The man who wrote 'all men are created equal' refused lace for his knees.",
                          "rarity": "uncommon"},
            "feet":      {"name": "Virginia Riding Boots",
                          "desc": "Soft leather, ankle-high. Carry the dust of every plantation road in the Commonwealth.",
                          "rarity": "common"},
            "amulet":    {"name": "Masonic Compass Pendant",
                          "desc": "A small brass compass on a cord. You square nothing without first measuring it.",
                          "rarity": "rare"},
            "ring":      {"name": "University of Virginia Signet",
                          "desc": "Silver. Seal of the academical village. A late, unfinished, beloved project.",
                          "rarity": "rare"},
        },
        "inventory": [
            {"name": "Letter from Madison",           "desc": "Unopened. You know it is about the Constitution and you are stalling.",          "rarity": "uncommon"},
            {"name": "Pocket Theodolite",             "desc": "For surveying the grounds of Monticello in the morning.",                         "rarity": "uncommon"},
            {"name": "Copy of the Notes on Virginia", "desc": "Your own. Annotated in your own handwriting. You argue with yourself.",            "rarity": "rare"},
            {"name": "Bottle of Bordeaux",            "desc": "1789. You were there when it was bottled.",                                        "rarity": "epic"},
        ],
    },

    # ── Kerrigan ───────────────────────────────────────────────────────────
    "kerrigan": {
        "class_title": "Psionic Infiltrator / Primal Queen",
        "alignment":   "Chaotic Neutral",
        "level":       20,
        "hp": 180, "hp_max": 180,
        "mp": 240, "mp_max": 240,
        "resource_label": "Psi",
        "stats": {"str": 16, "dex": 18, "con": 20, "int": 17, "wis": 16, "cha": 19},
        "equipment": {
            "main_hand": {"name": "Ascendant Psi-Claws",
                          "desc": "Three meters of curved bone, sheathed in a permanent psionic edge. They do not retract. They never have.",
                          "rarity": "legendary"},
            "off_hand":  {"name": "C-10 Canister Rifle",
                          "desc": "Ghost-issue. You kept it. You no longer need it. You keep it anyway.",
                          "rarity": "epic"},
            "head":      {"name": "Ghost HUD Visor",
                          "desc": "Optical cloak, thermal overlay, psi-dampener she rarely engages. Trophy from another life.",
                          "rarity": "rare"},
            "chest":     {"name": "Xel'Naga Resonant Carapace",
                          "desc": "Living chitin fused with crystal. It heals. Sometimes it sings back to its wearer.",
                          "rarity": "legendary"},
            "legs":      {"name": "Zergling-Tendon Greaves",
                          "desc": "Organic. Move silently across any surface. Regrow overnight if damaged.",
                          "rarity": "epic"},
            "feet":      {"name": "Null-Signature Drop Boots",
                          "desc": "Leave no heat trail, no pressure mark, no signature on any sensor she has yet met.",
                          "rarity": "rare"},
            "amulet":    {"name": "Khalai Focus Crystal",
                          "desc": "Salvaged from Aiur. Lets her speak protoss. She rarely does.",
                          "rarity": "epic"},
            "ring":      {"name": "Band of the Overmind (severed)",
                          "desc": "A tarnished ring of fused bone. The link inside it is dead. The scar is not.",
                          "rarity": "legendary"},
        },
        "inventory": [
            {"name": "Raynor's Dog Tags",             "desc": "She carries them in the one pocket that is not biological.",                 "rarity": "legendary"},
            {"name": "Vial of Primal Essence",        "desc": "Harvested on Zerus. Still warm.",                                            "rarity": "epic"},
            {"name": "Data Core (Confederate, enc.)", "desc": "Her own ghost file. She has never decrypted it.",                            "rarity": "rare"},
            {"name": "Field Rations (x12)",           "desc": "She does not need them. She takes them anyway, for the humans she protects.", "rarity": "common"},
        ],
    },
}


def get_sheet(persona_id: str) -> dict | None:
    """Return the character sheet for a persona id, or None if not defined."""
    return PERSONA_SHEETS.get(persona_id)


def all_sheet_ids() -> list[str]:
    return sorted(PERSONA_SHEETS.keys())
