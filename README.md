# EMBER FORGE

### *A voice-first AI companion RPG where every playthrough is unique.*

---

The Ember Forge reads you. It asks questions — strange ones, quiet ones, ones with no right answer. From your answers it shapes a companion who never existed before. Together you sit by a fire that wasn't burning until you arrived, and you discover what kind of story you're inside.

**This is not a game you win.** This is a game that remembers.

---

## What is this?

Ember Forge is a narrative roguelike companion game powered by AI. You build a party of unique companions, lead them through dice-driven encounters narrated by The Keeper (an eternal game master), forge mementos from shared moments, and face a world that gets harder the longer you rest.

Every companion has a unique voice. Every memento carries a sidequest. Every failure leaves a scar. Every departure — whether by brink or by becoming — leaves a relic behind. The game remembers everything.

**Key features:**
- 🎲 Hidden d20 dice system — the Keeper writes prose constrained by mechanical outcomes
- 🗡️ Turn-based encounters with drag-and-drop party ordering
- 💎 Void crystals + post-encounter reward cards (pick 1 of 3)
- 📜 Mementos with lifespans, sidequests, and three-state lifecycle
- ⚔️ Endgame ceremonies (The Becoming + Walk Into the Void)
- 🎙️ Voice chat with companions — push-to-talk, they respond in character
- 🔥 Procedural ambient soundscape that breathes with the game state
- 🌑 Ember Forge character creation — riddle questions generate a unique first companion
- ☁️ Cloud save sync (OneDrive/Dropbox/Google Drive)
- 🎮 Steam Deck compatible

---

## Quick Start (2 minutes)

### Requirements
- **Python 3.10+** ([download](https://python.org))
- **An Anthropic API key** ([get one](https://console.anthropic.com))
- **A modern browser** (Chrome, Edge, Firefox)

### Windows
1. Download or clone this repository
2. Double-click **`forge.bat`**
3. Enter your API key when prompted (first time only)
4. Play

### Mac / Linux / Steam Deck
```bash
git clone <repo-url>
cd wingman
python start.py
```

### From your phone / another device
After starting the server on your PC, open a browser on your other device:
```
http://<your-pc-ip>:7070/studio
```

---

## Settings

Open **Settings** (gear icon) to configure:

| Setting | Options |
|---|---|
| **LLM Provider** | Anthropic (recommended), OpenAI, Ollama (local/free), Together, Custom |
| **Character Voices** | Off, Browser (free), Edge TTS (free/good), ElevenLabs (paid/best) |
| **Save Directory** | Local (default) or cloud folder for cross-device sync |
| **Dice Feedback** | Off / Subtle / Visible |
| **Ambience** | Off / Subtle / Full |
| **Companion Hints** | On (discover together) / Off |

### Free to play
The only hard requirement is a language model API key. Everything else is optional:
- **No ElevenLabs needed** — browser TTS works fine
- **No OpenAI needed** — Ollama runs locally for free
- **No cloud account needed** — saves are local by default

---

## How it works

1. **The Forge reads you.** Answer riddle-questions. A companion is shaped from your answers.
2. **Camp.** Talk to your companion. Rest. Prepare. Discover.
3. **Sessions.** The Keeper narrates encounters. Dice determine outcomes. Stress accumulates.
4. **Mementos.** Shared moments crystallize into items with sidequests. Honor them or watch them fade.
5. **The world gets harder.** Rest costs pressure. Pressure raises difficulty. The losing battle is the game.
6. **Companions arrive.** New faces walk in when you're struggling. Each brings new abilities.
7. **Companions leave.** By brink (void) or by completion (becoming). Both leave relics. Both are real.
8. **Reward cards.** After every session: pick 1 of 3. Crystals are the currency of survival.
9. **The run ends** when your story does. Every run is different. Every companion is someone you'll remember.

---

## Credits

Built with Claude (Anthropic), ElevenLabs, and too much coffee.

*Every voice carries weight.*
