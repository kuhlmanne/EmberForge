"""
studio_server.py â€” Amplify Studio local server
Serves a live conversation window showing everything:
  - Eric's utterances (from Shadowheart's transcription)
  - Shadowheart's responses
  - Claude's deeper analysis when routed

WebSocket on ws://localhost:7070/ws
HTTP on http://localhost:7070

room_agent.py POSTs each turn to http://localhost:7070/turn
When a [CLAUDE: ...] tag is detected, routes to Anthropic and
streams the response back to all connected WebSocket clients.

Run: python studio_server.py
"""

import os, sys, json, time, threading, asyncio, logging
import urllib.request
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver

WINGMAN_DIR = r"C:\Users\kuhlm\Projects\wingman"
sys.path.insert(0, WINGMAN_DIR)

PORT      = 7070
WS_PORT   = 7071
LOG_FILE  = os.path.join(WINGMAN_DIR, "studio_server.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("studio")

def load_env():
    env = {}
    try:
        for line in open(os.path.join(WINGMAN_DIR, ".env"), encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except: pass
    return env

_env = load_env()
def get_env(k): return _env.get(k) or os.environ.get(k, "")

# Mirror critical keys into os.environ so imported modules (memento_forge,
# anthropic SDK, etc.) that read straight from os.environ find them.
for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY"):
    if _env.get(_k) and not os.environ.get(_k):
        os.environ[_k] = _env[_k]

# â”€â”€ Conversation store â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
conversation = []   # list of turn dicts
clients      = set()  # connected WebSocket-like clients (SSE)
clients_lock = threading.Lock()


# -- Health / biometric data from Galaxy Watch relay ---------------------------
# Updated via POST /health from phone/watch relay
# Fields: sleep_state (awake|light|deep|rem), heart_rate (bpm), updated_at (ISO)
_health_data = {
    "sleep_state": "unknown",
    "heart_rate": 0,
    "updated_at": "",
    "source": ""
}
_health_lock = threading.Lock()

# -- Room agent state (listening/capturing/processing/speaking) ----------------
_agent_state = {"state": "listening", "updated_at": "", "current_speaker": None}
_agent_state_lock = threading.Lock()

# -- Party widget state: muted personas (persisted) ---------------------------
_MUTE_FILE = os.path.join(WINGMAN_DIR, ".party_muted.json")
_mute_lock = threading.Lock()

def _load_muted() -> set:
    try:
        if os.path.exists(_MUTE_FILE):
            with open(_MUTE_FILE, "r", encoding="utf-8") as _mf:
                return set(json.load(_mf).get("muted", []))
    except Exception:
        pass
    return set()

def _save_muted(muted: set) -> None:
    try:
        with open(_MUTE_FILE, "w", encoding="utf-8") as _mf:
            json.dump({"muted": sorted(muted)}, _mf)
    except Exception:
        pass

_muted_personas = _load_muted()
# â”€â”€ Config store â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
CONFIG_FILE = os.path.join(WINGMAN_DIR, "studio_config.json")

try:
    import cost_router as _cost
    _COST_OK = True
except Exception:
    _COST_OK = False

# Persona framework — studio reads/writes persona state via these modules.
# The JSON files in personas/ and .persona_interactions.json are the source
# of truth; room_agent's hot-reload watcher picks up changes within 0.5s.
try:
    import persona_registry as _persona_registry
    _PERSONA_OK = True
except Exception as _pe:
    _PERSONA_OK = False

try:
    import interaction_store as _interaction_store
    _IX_OK = True
except Exception as _ie:
    _IX_OK = False

# Memento store + forge (CRPG inventory system; items generated from exchanges)
try:
    import memento_store as _memento_store
    import memento_forge as _memento_forge
    _MEMENTO_OK = True
except Exception as _me:
    _MEMENTO_OK = False

# Persona character sheets (static class kit: stats, equipment, level, class)
try:
    import persona_sheets as _persona_sheets
    _SHEETS_OK = True
except Exception as _pse:
    _SHEETS_OK = False

CONFIG_LOCK = threading.Lock()

DEFAULT_CONFIG = {
    "output_device":           "home",           # "home" (room agent/speakers) | "mobile" (Twilio call/SMS)
    "voice_mode":              "twilio",        # "elevenlabs" | "twilio" | "auto"
    "cost_tier":               "balanced",      # "economy" | "balanced" | "premium"
    "el_daily_budget_minutes": 30,              # EL minutes cap before auto-switch
    "el_minutes_used_today":   0,               # tracked by wingman, reset at midnight
    "el_budget_reset_hour":    0,               # hour (0-23) to reset daily EL counter
    "ambient_mode":            "active",        # "active" | "passive" | "off"
    "sn_auto_submit":          False,           # auto-submit tickets or queue for review
    "escalation_mode":         "claude",        # "off" | "claude" (SN tickets are voice-call only)
    "self_improve":            "propose",       # "auto" | "propose" | "off"
    "self_improve_scope":      ["infra"],       # ["infra", "config", "code"] â€” what she can touch
    "session_mode":            "normal",        # "normal" | "meeting" | "rehearsal" | "live_support"
    "session_costs":           {},              # per-mode overrides: {"meeting": {"model": "...", "max_tokens": N}}
    "vision_mode":             "off",           # "off" | "on_demand" | "periodic" | "active"
    "vision_throttle_seconds":  300,            # interval between vision reports (periodic mode)
    "screen_watcher":          "off",           # "off" | "gaming" | "work" | "passive" (auto-detects games)
    "game_active":             "",              # current game process name (set by screen_watcher)
    "game_executables":        [],              # custom game exe names to detect (in addition to defaults)
    "game_voice_volume":       0.39,            # 0.0–1.0 voice volume relative to game audio (0.39 = ~10% above legacy 0.35)
    "remote_mode":             "local",         # "local" | "crd" (Chrome Remote Desktop)
    "silent_mode":             "off",           # "on" | "off" — suppress non-addressed speech in CRD mode
    "log_level":               "info",          # "debug" | "info" | "warn" | "error"
    "tunnel_restart_threshold": 5,              # restart count/hour before escalating
    # ── Party / Personas (new) ──────────────────────────────────────────
    "party_idle_exchanges":        "off",       # "off" | "low" | "medium" | "high"
    "party_arc_memory":            True,        # persist cross-persona exchange arcs
    "party_drop_in_greeting":      True,        # persona greets on drop-in
    "party_default_exchange_tone": "riff",      # default tone when none specified
    "updated_at":              "",              # ISO timestamp of last config change
    "updated_by":              "",              # who changed it last
}

def load_config():
    """Load config from disk, merging with defaults for any missing keys."""
    with CONFIG_LOCK:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = {**DEFAULT_CONFIG, **saved}
            return merged
        except (FileNotFoundError, json.JSONDecodeError):
            return dict(DEFAULT_CONFIG)

def save_config(cfg, changed_by="unknown"):
    """Save config to disk and broadcast change via SSE."""
    cfg["updated_at"] = datetime.now().isoformat()
    cfg["updated_by"] = changed_by
    with CONFIG_LOCK:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    log.info(f"[Config] Saved by {changed_by}: voice_mode={cfg.get('voice_mode')} cost_tier={cfg.get('cost_tier')} self_improve={cfg.get('self_improve')}")
    try:
        broadcast("config_update", cfg)
    except NameError:
        pass  # broadcast not yet defined during module init
    return cfg

# Initialize config file if missing
if not os.path.exists(CONFIG_FILE):
    save_config(dict(DEFAULT_CONFIG), changed_by="studio_init")

# â”€â”€ Claude routing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
CLAUDE_SYSTEM = """You are Claude â€” the deep-analysis partner in Eric Kuhlmann's Amplify Studio.
Shadowheart handles the ambient verbal conversation. You handle depth, research, building, and synthesis.

Eric talks in his room. You see the full conversation. When something is routed to you:
- Give real substance. You have full context of Amplify, CBRE, and Eric's vision.
- If it's a build request, outline what to build and offer to start immediately.
- If it's research, give the actual answer with your reasoning visible.
- If it's synthesis, connect the dots across the conversation.
- No filler. No preamble. Eric hears Shadowheart verbally â€” you're the text layer.

Current time: {time}
Project context: Project Amplify â€” Eric's personal AI automation ecosystem on PNUT3.
Full stack: wingman (Twilio voice), watcher (relay), daemon (task executor), room_agent (Shadowheart ambient), studio (this window).
Vision: AI as exponential intelligence â€” distillation of collective human knowledge, accessible to anyone."""

def get_claude_response_streaming(query: str, conv_history: list):
    """Stream Claude response token by token via SSE."""
    api_key = get_env("ANTHROPIC_API_KEY")
    if not api_key:
        broadcast_stream_chunk("No API key configured.")
        broadcast_stream_done()
        return "No API key configured."

    recent = conv_history[-20:] if len(conv_history) > 20 else conv_history
    messages = []
    for turn in recent:
        if turn["role"] in ("eric", "shadowheart", "claude"):
            role = "user" if turn["role"] == "eric" else "assistant"
            messages.append({"role": role, "content": f"[{turn['role'].upper()}] {turn['text']}"})
    messages.append({"role": "user", "content": query})

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "system": CLAUDE_SYSTEM.format(time=datetime.now().strftime("%I:%M %p, %A")),
        "messages": messages
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    )
    full_text = ""
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line.startswith("data:"): continue
                raw = line[5:].strip()
                if raw == "[DONE]": break
                try:
                    evt = json.loads(raw)
                    if evt.get("type") == "content_block_delta":
                        chunk = evt.get("delta", {}).get("text", "")
                        if chunk:
                            full_text += chunk
                            broadcast_stream_chunk(chunk)
                except Exception:
                    pass
    except Exception as e:
        log.error(f"Claude stream error: {e}")
        broadcast_stream_chunk(f"\n[Error: {e}]")

    broadcast_stream_done()
    return full_text


def get_claude_response(query: str, conv_history: list) -> str:
    """Non-streaming fallback."""
    return get_claude_response_streaming(query, conv_history)


def broadcast(event_type: str, data: dict):
    """Send SSE event to all connected clients."""
    msg = f"data: {json.dumps({'type': event_type, **data})}\n\n"
    with clients_lock:
        dead = set()
        for q in clients:
            try:
                q.put(msg)
            except Exception:
                dead.add(q)
        for q in dead:
            clients.discard(q)


def broadcast_stream_chunk(text: str):
    broadcast("stream_chunk", {"text": text})


def broadcast_stream_done():
    broadcast("stream_done", {})


def add_turn(role: str, text: str, meta: str = ""):
    """Add a turn to conversation and broadcast to clients. Deduplicates within time window."""
    now_ts = time.time()
    # Reject duplicate: same role + same text within window
    # Bridge posts use longer window (10s) since fire-and-forget threads can arrive close together
    window = 10.0 if (meta and meta.startswith("bridge")) else 3.0
    if conversation:
        last = conversation[-1]
        if last["role"] == role and last["text"] == text and (now_ts - last.get("_ts", 0)) < window:
            log.debug(f"Duplicate suppressed: [{role}] {text[:40]}")
            return None
    turn = {
        "id": len(conversation),
        "role": role,
        "text": text,
        "meta": meta,
        "time": datetime.now().strftime("%H:%M:%S"),
        "_ts": now_ts,
    }
    conversation.append(turn)
    # Don't broadcast _ts to clients
    pub = {k: v for k, v in turn.items() if k != "_ts"}
    broadcast("turn", pub)
    log.info(f"[{role}] {text[:80]}")
    return turn


def handle_claude_routing(query: str):
    """Route query to Claude, stream to UI, then save as a turn."""
    log.info(f"Routing to Claude: {query[:60]}")
    # Note: routing indicator posted by room_agent before calling /ask â€” don't double-post
    response = get_claude_response_streaming(query, conversation)
    if response:
        add_turn("claude", response)
        return response
    return ""

# â”€â”€ HTTP Server â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import queue as queue_module

STUDIO_HTML_PATH = os.path.join(WINGMAN_DIR, "amplify_studio.html")

# Initialize game settings on startup — auto-imports .env keys
try:
    import game_settings as _gs
    _gs.load()
except Exception:
    pass
FORGE_MODE_INJECT = '<body data-mode="forge">'  # replaces <body> to hide non-game tabs


def _get_save_base_dir() -> str:
    """Return the save base directory — either a custom cloud-synced path
    from .save_dir.json or the default local .saves/ in WINGMAN_DIR."""
    cfg_path = os.path.join(WINGMAN_DIR, ".save_dir.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as _f:
                custom = json.load(_f).get("path", "").strip()
            if custom and os.path.isdir(custom):
                return custom
        except Exception:
            pass
    return os.path.join(WINGMAN_DIR, ".saves")


def _redirect_stores_to_slot(slot_dir: str) -> None:
    """Point ALL persistent state at a save-slot directory. This includes
    state files AND the persona registry's search path, so each slot is
    fully self-contained with its own personas + state. No bleed."""
    from pathlib import Path
    slot = Path(slot_dir)
    try:
        import stress_store
        stress_store.STORE_FILE = slot / ".persona_stress.json"
    except Exception: pass
    try:
        import memento_store
        memento_store.STORE_FILE = slot / ".persona_mementos.json"
    except Exception: pass
    try:
        import session_store
        session_store.STORE_FILE = slot / ".sessions.json"
    except Exception: pass
    try:
        import game_progression
        game_progression.STORE_FILE = slot / ".game_progression.json"
    except Exception: pass
    try:
        import camp_skills
        camp_skills.STORE_FILE = slot / ".persona_skills.json"
    except Exception: pass
    try:
        import reward_cards
        reward_cards.STORE_FILE = slot / ".void_crystals.json"
    except Exception: pass
    # Point persona_registry at the slot's personas/ dir so each slot
    # has its own set of companions (starters copied in + generated).
    try:
        import persona_registry
        slot_personas = slot / "personas"
        if slot_personas.exists():
            persona_registry.PERSONAS_DIR = slot_personas
            persona_registry.load()  # reload from new dir
    except Exception: pass


def _init_slot_personas(slot_dir: str) -> None:
    """Copy starter persona JSONs + portraits into a new slot's personas/
    directory. Called when creating a new save slot. After this, the slot
    is self-contained and persona_registry can load from it exclusively."""
    import shutil
    from pathlib import Path
    slot = Path(slot_dir)
    slot_personas = slot / "personas"
    slot_images = slot / "persona_images"
    slot_personas.mkdir(parents=True, exist_ok=True)
    slot_images.mkdir(parents=True, exist_ok=True)

    # Copy starter persona definitions
    shared_personas = Path(WINGMAN_DIR) / "personas"
    if shared_personas.exists():
        for f in shared_personas.glob("*.json"):
            # Skip non-persona config files
            if f.name.startswith("_") or f.name in ("edge_voice_sources.json", "el_voice_sources.json"):
                continue
            # Only copy the 5 starters, not previously generated personas
            starter_ids = {"shadowheart", "tesla", "laezel", "jefferson", "kerrigan"}
            stem = f.stem
            if stem in starter_ids:
                dest = slot_personas / f.name
                if not dest.exists():
                    shutil.copy2(str(f), str(dest))

    # Copy starter portraits
    shared_images = Path(WINGMAN_DIR) / "persona_images"
    if shared_images.exists():
        for f in shared_images.iterdir():
            if f.is_file():
                dest = slot_images / f.name
                if not dest.exists():
                    shutil.copy2(str(f), str(dest))


def _apply_reward_card_effect(effect: dict) -> None:
    """Apply the mechanical effect of a picked reward card. Called
    server-side after the crystal spend is confirmed. Effects that
    need player targeting (which persona to heal, which memento to
    revive) use the highest-stress or oldest-dormant as auto-targets
    for now; the UI pass will add targeting later."""
    eff_type = (effect or {}).get("type", "")
    try:
        import stress_store
        if eff_type == "heal_stress":
            # Heal the highest-stress alive persona
            all_s = stress_store.get_all_state().get("stress", {})
            departed = set(stress_store.departed_personas())
            alive = {p: int(v) for p, v in all_s.items() if p not in departed}
            if alive:
                worst = max(alive, key=lambda p: alive[p])
                amount = int(effect.get("amount", 3))
                stress_store.adjust_stress(worst, -amount)
        elif eff_type == "reduce_pressure":
            amount = int(effect.get("amount", 5))
            stress_store.adjust_world_pressure(-amount)
        elif eff_type == "resonance_boost":
            all_s = stress_store.get_all_state().get("stress", {})
            departed = set(stress_store.departed_personas())
            alive = [p for p in all_s if p not in departed]
            if alive:
                # Boost the persona closest to Becoming threshold
                best = min(alive, key=lambda p: abs(20 - stress_store.get_resonance(p)["total"]))
                stress_store.add_forged_resonance(best, int(effect.get("amount", 2)))
        elif eff_type == "grant_edge":
            all_s = stress_store.get_all_state().get("stress", {})
            departed = set(stress_store.departed_personas())
            alive = [p for p in all_s if p not in departed]
            if alive:
                stress_store.add_pending_edge(
                    alive[0],
                    name="Reward Edge",
                    desc=f"+{effect.get('bonus', 3)} to next roll",
                )
        elif eff_type == "trigger_arrival":
            import game_progression
            # This will be handled by the next camp tick / session start
            # For now just log intent; full arrival flow is Phase H
            pass
        elif eff_type == "crystal_rescue":
            brink = stress_store.personas_on_brink()
            if brink:
                target_stress = int(effect.get("target_stress", 10))
                stress_store.set_stress(brink[0], target_stress)
        elif eff_type == "revive_memento":
            import memento_store
            for pid in stress_store.get_all_state().get("stress", {}):
                dormant = memento_store.dormant_mementos_for(pid)
                if dormant:
                    memento_store.set_memento_status(dormant[0]["id"], "pending")
                    break
    except Exception as e:
        print(f"[reward] effect '{eff_type}' failed: {e}")


class StudioHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass  # suppress default logging

    def _check_auth(self) -> bool:
        """Allow localhost always. Require token for external access."""
        client_ip = self.client_address[0]
        # Localhost always allowed (IP-based, not Host header)
        if client_ip in ('127.0.0.1', '::1'):
            return True
        # Check token in query string or header
        studio_token = get_env("STUDIO_ACCESS_TOKEN")
        if not studio_token:
            return False  # no token configured = deny non-local
        # Check Authorization header or ?token= query param
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {studio_token}":
            return True
        if f"token={studio_token}" in self.path:
            return True
        return False

    def _send_auth_error(self):
        self.send_response(401)
        self.send_header("Content-Type", "text/plain")
        self.send_header("WWW-Authenticate", 'Bearer realm="Amplify Studio"')
        self.end_headers()
        self.wfile.write(b"Unauthorized -- provide ?token=YOUR_TOKEN or Authorization: Bearer YOUR_TOKEN")

    def do_GET(self):
        if not self._check_auth():
            self._send_auth_error()
            return
        # Normalize path: strip query string so exact-match routes work with
        # ?token=X appended (required via ngrok). _check_auth() already ran
        # against the FULL self.path so the token was still validated.
        # We assign to self.path so every `self.path ==` / `startswith` check
        # below works consistently.
        if "?" in self.path:
            self.path = self.path.split("?", 1)[0]
        if self.path == "/studio":
            # Full Amplify Studio — Eric's personal view with all tabs
            if os.path.exists(STUDIO_HTML_PATH):
                with open(STUDIO_HTML_PATH, encoding="utf-8") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            else:
                self.send_error(404, "Studio HTML not found")

        elif self.path == "/" or self.path == "/game":
            # Ember Forge game mode — no Room/Claude/All tabs, game UI only
            if os.path.exists(STUDIO_HTML_PATH):
                with open(STUDIO_HTML_PATH, encoding="utf-8") as f:
                    html = f.read()
                html = html.replace("<body>", '<body data-mode="forge">', 1)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            else:
                self.send_error(404, "Game HTML not found")

        elif self.path == "/forge":
            # Standalone Ember Forge landing page — synopsis + download.
            # This is the public-facing page shared via ngrok. The game
            # itself runs locally after download.
            landing = os.path.join(WINGMAN_DIR, "emberforge_landing.html")
            if os.path.exists(landing):
                with open(landing, encoding="utf-8") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            else:
                self.send_error(404, "Landing page not found")

        elif self.path == "/party_widget":
            # Pop-out party widget HTML (loaded by pywebview).
            _widget_path = os.path.join(WINGMAN_DIR, "party_widget.html")
            if os.path.exists(_widget_path):
                with open(_widget_path, encoding="utf-8") as _wf:
                    _widget_html = _wf.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(_widget_html.encode())
            else:
                self.send_error(404, "Party widget HTML not found")

        elif self.path in ("/ember-drift", "/brain-map"):
            # Serve Ember Drift dashboard
            ed_path = os.path.join(WINGMAN_DIR, "ember-drift.html")
            if os.path.exists(ed_path):
                with open(ed_path, encoding="utf-8") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            else:
                self.send_error(404, "Ember Drift HTML not found")

        elif self.path == "/events":
            # Server-Sent Events stream
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Send current conversation state + memory threads
            # Deduplicate for display â€” same role+text only shown once
            seen_keys = set()
            deduped = []
            for t in conversation:
                key = f"{t['role']}|{t['text']}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    deduped.append(t)
            try:
                from shadow_memory import load_memory
                mem = load_memory()
                open_threads = [t["thread"] for t in mem.get("open_threads", []) if not t.get("resolved")]
            except Exception:
                open_threads = []
            init_data = json.dumps({"type": "init", "conversation": deduped, "open_threads": open_threads, "tasks": get_live_tasks()})
            self.wfile.write(f"data: {init_data}\n\n".encode())
            self.wfile.flush()

            # Register this client
            q = queue_module.Queue()
            with clients_lock:
                clients.add(q)

            try:
                while True:
                    try:
                        msg = q.get(timeout=30)
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                    except queue_module.Empty:
                        # Heartbeat
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with clients_lock:
                    clients.discard(q)

        elif self.path == "/tunnel-url":
            # Returns current ngrok tunnel URL for Studio (port 4041)
            try:
                import urllib.request as _ur
                data = json.loads(_ur.urlopen("http://localhost:4041/api/tunnels", timeout=2).read())
                tunnels = data.get("tunnels", [])
                url = next((t["public_url"] for t in tunnels if "https" in t["public_url"]), None)
                token = get_env("STUDIO_ACCESS_TOKEN")
                if url and token:
                    url = f"{url}/?token={token}"
                resp = json.dumps({"url": url or "tunnel not running"}).encode()
            except Exception:
                resp = json.dumps({"url": "tunnel not running â€” run start-studio-tunnel.bat"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp)

        elif self.path == "/config":
            # Return current config as JSON
            cfg = load_config()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(cfg).encode())

        elif self.path == "/party/state":
            # Full persona + party + interaction state for the Camp tab UI.
            # Reads persona_registry (forcing a reload in case someone edited
            # a JSON file manually) and interaction_store.
            data = {"ok": False, "personas": [], "pairs": {}, "presence": {}}
            try:
                if _PERSONA_OK:
                    # Don't reload on every /party/state — only use cached.
                    # Reload happens on slot change + forge generation.
                    _all = _persona_registry.all_personas()
                    # Filter by active slot's arrival history if a slot
                    # is loaded. New game slots only show arrived personas.
                    try:
                        import game_progression as _gp
                        _gp_state = _gp.get_state()
                        _arrived_ids = set(a.get("persona_id") for a in _gp_state.get("arrival_history", []))
                        # If arrivals exist, filter to only those personas
                        # (legacy saves with no game_progression show everyone)
                        if _arrived_ids:
                            _all = [p for p in _all if p.id in _arrived_ids]
                    except Exception:
                        pass  # no filtering if game_progression unavailable
                    # Check both slot-specific + shared persona_images dirs
                    _img_dirs = [os.path.join(WINGMAN_DIR, "persona_images")]
                    try:
                        _slot_img = str(_persona_registry.PERSONAS_DIR).replace("personas", "persona_images")
                        if _slot_img not in _img_dirs:
                            _img_dirs.insert(0, _slot_img)
                    except Exception: pass
                    _personas_out = []
                    for _p in _all:
                        _has_portrait = any(
                            os.path.exists(os.path.join(_idir, f"{_p.id}.{_ext}"))
                            for _idir in _img_dirs
                            for _ext in ("jpg", "jpeg", "png", "webp")
                        )
                        with _mute_lock:
                            _is_muted = _p.id in _muted_personas
                        # CRPG character sheet (static class kit) + mementos (dynamic)
                        _sheet = _persona_sheets.get_sheet(_p.id) if _SHEETS_OK else None
                        _mementos = _memento_store.mementos_for(_p.id) if _MEMENTO_OK else []
                        # Compact affinity view for turn-strip tooltips
                        _affinity = None
                        try:
                            import persona_affinities as _paff
                            _affinity = _paff.get_affinity_for_client(_p.id)
                        except Exception:
                            pass
                        # Stress word for tooltip ("steady" / "tired" / etc.)
                        # Phase C: expanded scale now includes brink/shattered/lost
                        _stress_word = "steady"
                        _stress_val = 0
                        _brink_status = None
                        _resonance = {"safe": 0, "forged": 0, "total": 0}
                        _ready_for_becoming = False
                        _is_departed = False
                        _departed_info = None
                        try:
                            import stress_store as _stress
                            _stress_val = _stress.get_stress(_p.id)
                            _brink_status = _stress.brink_status_for(_p.id)
                            _resonance = _stress.get_resonance(_p.id)
                            _ready_for_becoming = _stress.is_ready_for_becoming(_p.id)
                            _is_departed = _stress.is_departed(_p.id)
                            if _is_departed:
                                _departed_info = _stress.departed_entry(_p.id)
                            # Legible stress word based on the expanded scale
                            if _brink_status == "lost":
                                _stress_word = "lost"
                            elif _brink_status == "shattered":
                                _stress_word = "shattered"
                            elif _brink_status == "brink":
                                _stress_word = "on the brink"
                            elif _stress_val >= 15:
                                _stress_word = "near breaking"
                            elif _stress_val >= 10:
                                _stress_word = "carrying weight"
                            elif _stress_val >= 5:
                                _stress_word = "tired"
                            else:
                                _stress_word = "steady"
                        except Exception:
                            pass
                        _personas_out.append({
                            "id": _p.id,
                            "display_name": _p.display_name,
                            "origin": _p.origin,
                            "era": _p.era,
                            "status": _p.status,
                            "archetype": _p.archetype,
                            "essence": _p.essence,
                            "profile_image": _p.profile_image,
                            "has_portrait": _has_portrait,
                            "muted": _is_muted,
                            "wake_aliases_count": len(_p.wake_aliases),
                            "wake_aliases_sample": _p.wake_aliases[:3],
                            "voice_engine": (_p.voice or {}).get("engine", "?"),
                            "voice_sample": (_p.voice or {}).get("sample", ""),
                            "relationships_keys": list(_p.relationships.keys()),
                            "relationships": _p.relationships,
                            "quirks": _p.quirks,
                            "quirks_count": len(_p.quirks),
                            "signature_quotes": _p.signature_quotes,
                            "signature_quotes_count": len(_p.signature_quotes),
                            "sheet": _sheet,          # CRPG class kit (or None)
                            "mementos": _mementos,    # dynamic inventory from exchanges
                            "affinity": _affinity,    # turn-strip icons + tags
                            "stress": _stress_val,    # raw stress value
                            "stress_word": _stress_word,  # legible stress label
                            # Phase C additions:
                            "brink_status": _brink_status,           # null|"brink"|"shattered"|"lost"
                            "resonance": _resonance,                 # {safe, forged, total}
                            "ready_for_becoming": _ready_for_becoming,
                            "departed": _is_departed,
                            "departed_info": _departed_info,
                        })
                    data["personas"] = _personas_out
                    data["ok"] = True
                if _IX_OK:
                    _store = _interaction_store._load()
                    # Pairs with readable relative times
                    _pairs_out = {}
                    for _key, _pair in _store.get("pairs", {}).items():
                        _p2 = dict(_pair)
                        if _p2.get("last_ts"):
                            _p2["last_relative"] = _interaction_store.format_relative_time(
                                time.time() - _p2["last_ts"])
                        _pairs_out[_key] = _p2
                    data["pairs"] = _pairs_out
                    # Presence with computed absence durations
                    _presence_out = {}
                    for _pid, _entry in _store.get("personas", {}).items():
                        _out = dict(_entry)
                        _last_exit = _interaction_store.seconds_since_last_party_exit(_pid)
                        if _last_exit is not None:
                            _out["last_party_exit_relative"] = _interaction_store.format_relative_time(_last_exit)
                        _first_seen = _interaction_store.seconds_since_first_seen(_pid)
                        if _first_seen is not None:
                            _out["first_seen_relative"] = _interaction_store.format_relative_time(_first_seen)
                        _presence_out[_pid] = _out
                    data["presence"] = _presence_out
            except Exception as _e:
                data["error"] = str(_e)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())

        elif self.path == "/party/brink_status":
            # GET /party/brink_status
            # Lightweight polling endpoint for the UI. Returns the set of
            # personas currently available for a ritual (either brink or
            # becoming) so the camp view can light up their portraits.
            # Full persona data lives on /party/state; this one is cheap.
            try:
                import stress_store
                state = {
                    "ok": True,
                    "on_brink":            stress_store.personas_on_brink(),
                    "ready_for_becoming":  stress_store.personas_ready_for_becoming(),
                    "departed":            stress_store.departed_personas(),
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(state).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path.startswith("/party/portrait/"):
            # Serves persona_images/<id>.{jpg,png}. Used by the Camp tab cards.
            _pid = self.path[len("/party/portrait/"):].split("?")[0]
            # Whitelist: only ids that exist in the registry (prevents path traversal)
            if not _PERSONA_OK or not _persona_registry.get(_pid):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"unknown persona")
                return
            # Check slot-specific persona_images first, then shared
            _img_path = None
            _search_dirs = []
            try:
                import persona_registry as _preg
                _slot_images = str(_preg.PERSONAS_DIR).replace("personas", "persona_images")
                _search_dirs.append(_slot_images)
            except Exception: pass
            _search_dirs.append(os.path.join(WINGMAN_DIR, "persona_images"))
            for _images_dir in _search_dirs:
                for _ext in ("jpg", "jpeg", "png", "webp"):
                    _candidate = os.path.join(_images_dir, f"{_pid}.{_ext}")
                    if os.path.exists(_candidate):
                        _img_path = _candidate
                        break
                if _img_path: break
            if not _img_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"no portrait")
                return
            _ext = os.path.splitext(_img_path)[1].lower().lstrip(".")
            _mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "png": "image/png", "webp": "image/webp"}.get(_ext, "application/octet-stream")
            with open(_img_path, "rb") as _f:
                _data = _f.read()
            self.send_response(200)
            self.send_header("Content-Type", _mime)
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(_data)

        elif self.path == "/saves/list":
            # GET /saves/list — return metadata for all 3 save slots.
            # Slot 1 auto-detects existing root state files (legacy save
            # from before the slot system existed). If root has personas
            # but no slot_1 directory, Slot 1 shows the root state.
            slots = {}
            _save_base = _get_save_base_dir()
            for slot_id in [1, 2, 3]:
                slot_dir = os.path.join(_save_base, f"slot_{slot_id}")
                gp_file = os.path.join(slot_dir, ".game_progression.json")
                forge_file = os.path.join(slot_dir, ".forge_answers.json")
                if os.path.exists(gp_file):
                    try:
                        with open(gp_file, "r", encoding="utf-8") as _f:
                            gp = json.load(_f)
                        player_name = "Traveler"
                        if os.path.exists(forge_file):
                            with open(forge_file, "r", encoding="utf-8") as _f:
                                fa = json.load(_f)
                            player_name = fa.get("player_name", "Traveler")
                        slots[str(slot_id)] = {
                            "session_count": gp.get("session_count", 0),
                            "library_size":  gp.get("library_size", 0),
                            "library":       [a.get("persona_id", "?") for a in gp.get("arrival_history", [])],
                            "player_name":   player_name,
                        }
                    except Exception:
                        slots[str(slot_id)] = {}
                else:
                    slots[str(slot_id)] = {}

            # Auto-detect legacy root state for Slot 1 if slot_1 is empty
            if not slots.get("1", {}).get("session_count"):
                root_stress = os.path.join(WINGMAN_DIR, ".persona_stress.json")
                if os.path.exists(root_stress):
                    try:
                        # Count personas from the registry
                        if _PERSONA_OK:
                            _persona_registry.load()
                            all_p = _persona_registry.all_personas()
                            party = [p for p in all_p if p.status in ("party", "camp")]
                            if party:
                                slots["1"] = {
                                    "session_count": -1,  # flag: legacy save
                                    "library_size": len(party),
                                    "library": [p.id for p in party],
                                    "player_name": "Eric (legacy save)",
                                    "legacy": True,
                                }
                    except Exception:
                        pass

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "slots": slots}).encode())
            return

        elif self.path == "/download/setup":
            # GET /download/setup — serve the one-click setup bat
            setup_path = os.path.join(WINGMAN_DIR, "EmberForge_Setup.bat")
            if os.path.exists(setup_path):
                with open(setup_path, "r", encoding="utf-8") as _f:
                    content = _f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=EmberForge_Setup.bat")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(content.encode())
            else:
                self.send_response(404)
                self.end_headers()
            return

        elif self.path == "/download/zip":
            # GET /download/zip — serve EmberForge.zip (Chrome-friendly)
            zip_path = os.path.join(WINGMAN_DIR, "dist", "EmberForge.zip")
            if os.path.exists(zip_path):
                size = os.path.getsize(zip_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", "attachment; filename=EmberForge.zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(zip_path, "rb") as _f:
                    shutil.copyfileobj(_f, self.wfile)
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"EmberForge.zip not built yet.")
            return

        elif self.path == "/download/exe":
            # GET /download/exe — serve EmberForge.exe for download
            exe_path = os.path.join(WINGMAN_DIR, "dist", "EmberForge.exe")
            if os.path.exists(exe_path):
                size = os.path.getsize(exe_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=EmberForge.exe")
                self.send_header("Content-Length", str(size))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(exe_path, "rb") as _f:
                    shutil.copyfileobj(_f, self.wfile)
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"EmberForge.exe not built yet. Run: python build_forge.py")
            return

        elif self.path == "/install":
            # GET /install — serve the installer Python script for download
            installer_path = os.path.join(WINGMAN_DIR, "install_emberforge.py")
            if os.path.exists(installer_path):
                with open(installer_path, "r", encoding="utf-8") as _f:
                    content = _f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=install_emberforge.py")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(content.encode())
            else:
                self.send_response(404)
                self.end_headers()
            return

        elif self.path == "/system/save_dir":
            # GET /system/save_dir — return the configured save directory
            try:
                cfg_path = os.path.join(WINGMAN_DIR, ".save_dir.json")
                path = ""
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8") as _f:
                        path = json.load(_f).get("path", "")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"path": path}).encode())
            except Exception as _e:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"path": ""}).encode())
            return

        elif self.path == "/system/settings":
            # GET /system/settings — return all game settings (keys masked)
            try:
                import game_settings
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(game_settings.get_safe()).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(_e)}).encode())
            return

        elif self.path == "/system/api_key_status":
            # GET /system/api_key_status — check if ANTHROPIC_API_KEY is set
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"has_key": has_key}).encode())
            return

        elif self.path == "/system/steam_check":
            # GET /system/steam_check — silently detect if Steam is installed.
            # If not, the Ember Forge won't offer the Steam integration question.
            installed = False
            try:
                steam_paths = [
                    r"C:\Program Files (x86)\Steam\steam.exe",
                    r"C:\Program Files\Steam\steam.exe",
                    os.path.expanduser(r"~\Steam\steam.exe"),
                ]
                installed = any(os.path.exists(p) for p in steam_paths)
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"installed": installed}).encode())
            return

        elif self.path == "/reward/economy":
            # GET /reward/economy - crystal count + pending cards + recent picks
            try:
                import reward_cards
                state = reward_cards.get_economy_state()
                state["ok"] = True
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(state).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session_costs":
            # Return per-session-mode cost configs
            if _COST_OK:
                data = _cost.get_session_costs_summary()
            else:
                data = {"error": "cost_router not loaded"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/cost":
            # Return real-time cost summary from cost_tracker (Python session)
            # plus JS-side rolling windows from cost_intelligence via wingman :3001
            data = {}
            if _COST_OK:
                try:
                    import cost_tracker as _ct
                    data["session_py"] = _ct.get_summary()
                    data["formatted"]  = _ct.format_summary()
                except Exception as _e:
                    data["session_py"] = {"error": str(_e)}
            try:
                req = urllib.request.Request(
                    "http://localhost:3001/cost", method="GET"
                )
                with urllib.request.urlopen(req, timeout=2) as r:
                    data["js"] = json.loads(r.read())
            except Exception:
                data["js"] = {"error": "wingman not running or /cost not exposed yet"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())

        elif self.path == "/state":
            # Return current conversation as JSON
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"conversation": conversation}).encode())


        elif self.path == "/health":
            # Return current health/biometric data
            with _health_lock:
                data = dict(_health_data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/agent_state":
            with _agent_state_lock:
                data = dict(_agent_state)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/twiml/car":
            # TwiML for Twilio incoming calls — connects to car_agent WebSocket
            try:
                from car_agent import generate_twiml_connect
                ngrok_url = get_env("NGROK_URL") or "wss://localhost:8766"
                ws_url = ngrok_url.replace("https://", "wss://").rstrip("/") + "/car"
                twiml = generate_twiml_connect(ws_url)
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(twiml.encode())
            except Exception as e:
                log.error(f"TwiML car error: {e}")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        elif self.path == "/social/threads":
            # Return active social conversation threads
            try:
                from social_agent import load_conversations
                convos = load_conversations()
                summary = {}
                for phone, data in convos.items():
                    summary[phone] = {
                        "name": data.get("name", "Unknown"),
                        "last_contact": data.get("last_contact", ""),
                        "message_count": len(data.get("messages", [])),
                        "manual_mode": data.get("manual_mode", False),
                        "pending_meetup": data.get("pending_meetup"),
                    }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(summary).encode())
            except Exception as e:
                log.error(f"Social threads error: {e}")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

        else:
            self.send_error(404)

    def do_POST(self):
        if not self._check_auth():
            self._send_auth_error()
            return
        # Note: CORS header moved into each response path (send_header before send_response is invalid)

        # Normalize path: strip query string so exact-match routes work with
        # ?token=X appended (required via ngrok). _check_auth() already ran
        # against the FULL self.path so the token was still validated.
        if "?" in self.path:
            self.path = self.path.split("?", 1)[0]

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if self.path == "/turn":
            role = data.get("role", "unknown")
            text = data.get("text", "")
            meta = data.get("meta", "")
            extra = {k: v for k, v in data.items() if k not in ("role","text","meta")}

            # Cost updates: skip conversation log, just broadcast SSE directly
            if meta == "cost_update" and "cost" in extra:
                broadcast("cost_update", {"cost": extra["cost"]})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return

            add_turn(role, text, meta)
            import re
            claude_match = re.search(r'\[CLAUDE:\s*(.+?)\]', text, re.IGNORECASE | re.DOTALL)
            if claude_match:
                query = claude_match.group(1).strip()
                threading.Thread(target=handle_claude_routing, args=(query,), daemon=True).start()

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        elif self.path == "/ask":
            # Claude query â€” from sidebar UI or room_agent programmatic routing
            query  = data.get("query", "")
            source = data.get("source", "ui")  # "ui" = sidebar, "room" = room_agent
            if query:
                if source == "ui":
                    # Sidebar query â€” show as eric turn
                    add_turn("eric", f"[to Claude] {query}")
                # Always route to Claude
                threading.Thread(target=handle_claude_routing, args=(query,), daemon=True).start()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        elif self.path == "/ask_sync":
            # Synchronous Claude query â€” waits for response and returns it
            # Used by room_agent so Shadowheart can speak Claude's reply
            query = data.get("query", "")
            if query:
                response = handle_claude_routing(query)
                resp_bytes = json.dumps({"ok": True, "response": response or ""}).encode()
            else:
                resp_bytes = b'{"ok":false,"response":""}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_bytes)
            return
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        elif self.path == "/config":
            # Update config â€” partial updates supported (merge with existing)
            current = load_config()
            changed_by = data.pop("changed_by", "api")
            # Only update keys that are in DEFAULT_CONFIG (ignore unknown keys)
            for key in data:
                if key in DEFAULT_CONFIG:
                    current[key] = data[key]
            cfg = save_config(current, changed_by=changed_by)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "config": cfg}).encode())
            return

        elif self.path == "/party/drop_in":
            # Body: {"id": "tesla"}  — move persona from camp to party
            resp = {"ok": False}
            if _PERSONA_OK:
                _pid = (data.get("id") or "").strip()
                if _pid:
                    try:
                        _persona_registry.load()  # refresh before mutating
                        _ok = _persona_registry.drop_in(_pid)
                        resp = {
                            "ok": _ok,
                            "party": [p.id for p in _persona_registry.party()],
                        }
                    except Exception as _e:
                        resp = {"ok": False, "error": str(_e)}
                else:
                    resp = {"ok": False, "error": "missing id"}
            self.send_response(200 if resp.get("ok") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            return

        elif self.path == "/party/drop_out":
            # Body: {"id": "tesla"}  — move persona from party to camp
            resp = {"ok": False}
            if _PERSONA_OK:
                _pid = (data.get("id") or "").strip()
                if _pid:
                    try:
                        _persona_registry.load()
                        _ok = _persona_registry.drop_out(_pid)
                        resp = {
                            "ok": _ok,
                            "party": [p.id for p in _persona_registry.party()],
                        }
                    except Exception as _e:
                        resp = {"ok": False, "error": str(_e)}
                else:
                    resp = {"ok": False, "error": "missing id"}
            self.send_response(200 if resp.get("ok") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            return

        elif self.path == "/party/exchange":
            # Body: {"a": "laezel", "b": "kerrigan", "kind": "argue",
            #        "topic": "...", "rounds": 3}
            # Writes .party_exchange_request.json which the room_agent
            # watcher picks up in ~0.5s.
            _a = (data.get("a") or "").strip()
            _b = (data.get("b") or "").strip()
            _kind = (data.get("kind") or "riff").strip()
            _topic = data.get("topic")
            _rounds = int(data.get("rounds", 3))
            resp = {"ok": False}
            if not _a or not _b:
                resp = {"ok": False, "error": "missing a or b"}
            else:
                try:
                    _trigger = {"a": _a, "b": _b, "kind": _kind, "rounds": _rounds}
                    if _topic:
                        _trigger["topic"] = _topic
                    _trig_path = os.path.join(WINGMAN_DIR, ".party_exchange_request.json")
                    with open(_trig_path, "w", encoding="utf-8") as _tf:
                        json.dump(_trigger, _tf)
                    resp = {"ok": True, "queued": _trigger}
                except Exception as _e:
                    resp = {"ok": False, "error": str(_e)}
            self.send_response(200 if resp.get("ok") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            return

        elif self.path == "/party/mementos/backfill":
            # For each existing pair that has an arc summary but no memento,
            # forge one retroactively. Body: {} or {"pair_key": "a+b"} to
            # target a single pair.
            resp = {"ok": False, "forged": 0, "skipped": 0}
            if not _MEMENTO_OK or not _IX_OK or not _PERSONA_OK:
                resp = {"ok": False, "error": "memento/interaction/persona module not loaded"}
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(resp).encode())
                return
            try:
                _target_key = (data.get("pair_key") or "").strip() or None
                _store_data = _interaction_store._load()
                _pairs = _store_data.get("pairs", {})
                _existing = _memento_store.all_mementos()
                _existing_by_pair = {}
                for _m in _existing:
                    _existing_by_pair.setdefault(_m.get("pair_key"), []).append(_m)
                _forged = 0
                _skipped = 0
                for _pk, _pair in _pairs.items():
                    if _target_key and _pk != _target_key:
                        continue
                    _summary = _pair.get("last_summary")
                    if not _summary:
                        _skipped += 1
                        continue
                    if _existing_by_pair.get(_pk):
                        _skipped += 1
                        continue
                    _a_id = _pair.get("a")
                    _b_id = _pair.get("b")
                    _pa = _persona_registry.get(_a_id)
                    _pb = _persona_registry.get(_b_id)
                    if not _pa or not _pb:
                        _skipped += 1
                        continue
                    _a_dict = {"id": _pa.id, "display_name": _pa.display_name, "essence": _pa.essence}
                    _b_dict = {"id": _pb.id, "display_name": _pb.display_name, "essence": _pb.essence}
                    # Guess the kind from the summary if stored; default to riff
                    _kind = _pair.get("last_kind") or "riff"
                    _m = _memento_forge.forge_memento(
                        _a_dict, _b_dict, _kind, _summary,
                        int(_pair.get("count", 1)),
                    )
                    _memento_store.add_memento(_m)
                    _forged += 1
                resp = {"ok": True, "forged": _forged, "skipped": _skipped}
            except Exception as _be:
                resp = {"ok": False, "error": str(_be)}
            self.send_response(200 if resp.get("ok") else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            return

        elif self.path == "/party/camp_tick":
            # POST /party/camp_tick {}
            # For every persona currently in Camp (status=camp) who is past
            # their per-persona cooldown, roll a hidden d20. Results update
            # stress + maybe mint a healing memento on a critical (nat 20).
            # Returns a list of events the client can surface as toasts.
            result = {"ok": False, "events": []}
            try:
                import stress_store
                if not _PERSONA_OK:
                    raise RuntimeError("persona registry unavailable")
                _persona_registry.load()
                _camp_personas = [p for p in _persona_registry.all_personas() if p.status == "camp"]
                events = []
                for _p in _camp_personas:
                    if not stress_store.can_roll_camp(_p.id):
                        continue
                    current_stress = stress_store.get_stress(_p.id)
                    if current_stress <= 0:
                        # No stress to heal - skip rolling but still honor cooldown reset?
                        # We DO NOT set last_roll_ts here so the persona stays eligible.
                        continue
                    roll_event = stress_store.roll_for_camp_rest(_p.id)
                    if not roll_event.get("eligible"):
                        continue
                    _roll_val = int(roll_event.get("roll", 0))
                    event_out = {
                        "persona_id":     _p.id,
                        "persona_name":   _p.display_name,
                        "kind":           roll_event.get("kind"),
                        "critical":       roll_event.get("critical"),
                        "stress_before":  roll_event.get("stress_before"),
                        "stress_after":   roll_event.get("stress_after"),
                        "flavor":         None,
                        "edge":           None,
                        "memento":        None,
                    }

                    # Pull inner weight brief for any forge call below
                    _inner_weight = ""
                    try:
                        import session_forge as _sf
                        _inner_weight = _sf._INNER_WEIGHT_BRIEFS.get(_p.id, "")
                    except Exception:
                        pass
                    _persona_dict = {
                        "id":           _p.id,
                        "display_name": _p.display_name,
                        "essence":      _p.essence,
                    }

                    # Flavor vignette for any roll >= 11 (everything that
                    # wasn't a total miss). Haiku-generated one-sentence
                    # beat about what the persona did during rest.
                    if _roll_val >= 11 and _MEMENTO_OK:
                        try:
                            import memento_forge
                            _flavor = memento_forge.forge_camp_flavor(
                                persona=_persona_dict,
                                stress_level=event_out["stress_before"],
                                inner_weight=_inner_weight,
                            )
                            if _flavor:
                                event_out["flavor"] = _flavor
                                stress_store.append_flavor_to_latest_event(_p.id, _flavor)
                        except Exception as _ff:
                            log.warning(f"[camp_tick] flavor forge failed for {_p.id}: {_ff}")

                    # Temporary edge on roll 18-19 (rare, narrative advantage)
                    if 18 <= _roll_val <= 19 and _MEMENTO_OK:
                        try:
                            import memento_forge
                            _edge = memento_forge.forge_camp_edge(
                                persona=_persona_dict,
                                stress_level=event_out["stress_before"],
                                inner_weight=_inner_weight,
                            )
                            if _edge and _edge.get("name"):
                                stress_store.add_pending_edge(_p.id, _edge["name"], _edge.get("desc", ""))
                                stress_store.append_edge_ref_to_latest_event(_p.id, _edge)
                                event_out["edge"] = _edge
                                log.info(f"[camp_tick] {_p.id} forged edge: {_edge['name']}")
                        except Exception as _ee:
                            log.warning(f"[camp_tick] edge forge failed for {_p.id}: {_ee}")

                    # Healing memento on nat-20 critical
                    if roll_event.get("critical") and _MEMENTO_OK:
                        try:
                            import memento_forge
                            import memento_store as _ms
                            _minted = memento_forge.forge_healing_memento(
                                persona=_persona_dict,
                                stress_level=event_out["stress_before"],
                                inner_weight=_inner_weight,
                                affliction=stress_store.get_affliction(_p.id),
                            )
                            _stored = _ms.add_memento(_minted)
                            event_out["memento"] = {
                                "id":         _stored.get("id"),
                                "name":       _stored.get("name"),
                                "desc":       _stored.get("desc"),
                                "rarity":     _stored.get("rarity"),
                                "flavor_tag": _stored.get("flavor_tag"),
                            }
                            log.info(f"[camp_tick] {_p.id} CRIT heal -> {_stored.get('name')}")
                        except Exception as _fe:
                            log.warning(f"[camp_tick] healing forge failed for {_p.id}: {_fe}")

                    events.append(event_out)
                    log.info(f"[camp_tick] {_p.id} rolled {_roll_val} "
                             f"stress {event_out['stress_before']}->{event_out['stress_after']}"
                             f"{' flavor' if event_out['flavor'] else ''}"
                             f"{' EDGE' if event_out['edge'] else ''}"
                             f"{' MEMENTO' if event_out['memento'] else ''}")
                result = {"ok": True, "events": events, "rolled": len(events)}
            except Exception as _e:
                log.error(f"[camp_tick] failed: {_e}")
                result = {"ok": False, "error": str(_e)}
            self.send_response(200 if result.get("ok") else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(result, default=str).encode())
            return

        elif self.path == "/party/stress/set":
            # POST /party/stress/set {"id": "...", "value": 0..30}
            # Admin / test route - set a persona's stress to a specific value.
            try:
                import stress_store
                pid = (data.get("id") or "").strip()
                val = int(data.get("value", 0))
                new_val = stress_store.set_stress(pid, val)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "id": pid, "stress": new_val}).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/party/rites/becoming":
            # POST /party/rites/becoming {"id": "shadowheart"}
            # Fires The Becoming ceremony for a persona whose resonance has
            # reached the threshold. Returns {farewell, relic, template,
            # transformed_count}. The persona is marked departed on success.
            try:
                import session_forge
                pid = (data.get("id") or "").strip()
                if not pid:
                    raise ValueError("missing persona id")
                result = session_forge.perform_becoming(pid)
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/party/rites/void":
            # POST /party/rites/void {"id": "laezel"}
            # Fires Walk Into Void for a persona on the brink. Always uses the
            # full-weight void template. Returns {farewell, relic, template,
            # transformed_count}. Persona marked departed on success.
            try:
                import session_forge
                pid = (data.get("id") or "").strip()
                if not pid:
                    raise ValueError("missing persona id")
                result = session_forge.perform_walk_into_void(pid)
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/party/rites/rescue":
            # POST /party/rites/rescue {"id": "laezel"}
            # Called after a camp event or session outcome drops a brinked
            # persona below BRINK_THRESHOLD. Records the "what_almost_happened"
            # permanent scar and transitions out of brink. The player isn't
            # the trigger for this - it fires automatically when conditions
            # are met. Route exists for admin / testing.
            try:
                import stress_store
                pid = (data.get("id") or "").strip()
                if not pid:
                    raise ValueError("missing persona id")
                result = stress_store.rescue_from_brink(pid)
                self.send_response(200 if result.get("ok") else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/party/memento/release":
            # POST /party/memento/release {"id": "<memento_id>"}
            # Release a dormant memento to reduce world pressure by 2.
            try:
                import memento_store
                mid = (data.get("id") or "").strip()
                if not mid:
                    raise ValueError("missing memento id")
                result = memento_store.release_memento(mid)
                self.send_response(200 if result.get("ok") else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/party/memento/sell":
            # POST /party/memento/sell {"id": "<memento_id>"}
            # Sell a dormant memento for XP (scaled by rarity).
            try:
                import memento_store
                mid = (data.get("id") or "").strip()
                if not mid:
                    raise ValueError("missing memento id")
                result = memento_store.sell_memento(mid)
                self.send_response(200 if result.get("ok") else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/system/save_dir":
            # POST /system/save_dir {"path": "C:\\Users\\kuhlm\\OneDrive\\AmplifyData"}
            # Set the save directory. Creates it if it doesn't exist. All
            # future /saves/list, /saves/new, /saves/load use this as the
            # base instead of WINGMAN_DIR/.saves/.
            try:
                path = (data.get("path") or "").strip()
                cfg_path = os.path.join(WINGMAN_DIR, ".save_dir.json")
                if path:
                    os.makedirs(path, exist_ok=True)
                with open(cfg_path, "w", encoding="utf-8") as _f:
                    json.dump({"path": path}, _f)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "path": path}).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/system/settings":
            # POST /system/settings {key: value, ...} — update game settings
            try:
                import game_settings
                game_settings.update(data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/system/api_key_set":
            # POST /system/api_key_set {"key": "sk-ant-..."}
            # Save the API key to the .env file and set it in os.environ.
            # The key never leaves this machine.
            try:
                key = (data.get("key") or "").strip()
                if not key or not key.startswith("sk-"):
                    raise ValueError("Key must start with sk-")
                # Set in current process
                os.environ["ANTHROPIC_API_KEY"] = key
                # Persist to .env (append or replace)
                env_path = os.path.join(WINGMAN_DIR, ".env")
                lines = []
                replaced = False
                if os.path.exists(env_path):
                    with open(env_path, "r", encoding="utf-8") as _f:
                        for line in _f:
                            if line.strip().startswith("ANTHROPIC_API_KEY="):
                                lines.append(f"ANTHROPIC_API_KEY={key}\n")
                                replaced = True
                            else:
                                lines.append(line)
                if not replaced:
                    lines.append(f"ANTHROPIC_API_KEY={key}\n")
                with open(env_path, "w", encoding="utf-8") as _f:
                    _f.writelines(lines)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
            except Exception as _e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/saves/new":
            # POST /saves/new {"slot": 1}
            # Create a fresh save slot — wipe the slot directory and redirect
            # all state stores to it. The Ember Forge will write into this slot.
            try:
                slot_id = int(data.get("slot", 1))
                slot_dir = os.path.join(_get_save_base_dir(), f"slot_{slot_id}")
                # Wipe existing slot if any
                import shutil
                if os.path.exists(slot_dir):
                    shutil.rmtree(slot_dir, ignore_errors=True)
                os.makedirs(slot_dir, exist_ok=True)
                _init_slot_personas(slot_dir)  # copy starters into the slot
                _redirect_stores_to_slot(slot_dir)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "slot": slot_id, "path": slot_dir}).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/saves/load":
            # POST /saves/load {"slot": 1}
            # Load an existing save slot. If slot 1 is a legacy save (root
            # state files), point stores at the root WINGMAN_DIR instead of
            # a subdirectory.
            try:
                slot_id = int(data.get("slot", 1))
                # Check if this is the legacy root save
                slot_dir_check = os.path.join(_get_save_base_dir(), f"slot_{slot_id}")
                gp_check = os.path.join(slot_dir_check, ".game_progression.json")
                is_legacy = (slot_id == 1 and not os.path.exists(gp_check)
                             and os.path.exists(os.path.join(WINGMAN_DIR, ".persona_stress.json")))
                if is_legacy:
                    # Point stores at root (where existing state lives)
                    _redirect_stores_to_slot(WINGMAN_DIR)
                    slot_dir = WINGMAN_DIR
                else:
                    slot_dir = slot_dir_check
                    os.makedirs(slot_dir, exist_ok=True)
                    _redirect_stores_to_slot(slot_dir)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "slot": slot_id, "legacy": is_legacy}).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/voice/chat":
            # POST /voice/chat {"persona_id": "shadowheart", "transcript": "hey",
            #   "location": "camp", "history": [{role, content}, ...]}
            # Voice chat with a companion. Returns {text, persona_id, display_name}.
            try:
                import game_voice
                pid = (data.get("persona_id") or "").strip()
                transcript = (data.get("transcript") or "").strip()
                location = (data.get("location") or "camp").strip()
                history = data.get("history") or []
                if not pid:
                    raise ValueError("missing persona_id")
                if not transcript:
                    raise ValueError("empty transcript")
                result = game_voice.chat(pid, transcript, location, history)
                # If the persona has an ElevenLabs voice, generate audio
                if result.get("ok") and result.get("text"):
                    try:
                        import voice_forge, persona_registry
                        p = persona_registry.get(pid)
                        if p and hasattr(p, '_raw'):
                            vid = (p._raw.get("voice") or {}).get("voice_id")
                        elif p:
                            vid = (getattr(p, 'voice', None) or {}).get("voice_id")
                        else:
                            vid = None
                        if not vid:
                            # Check the persona JSON directly — look in slot dir first
                            _search_dirs = []
                            try:
                                _search_dirs.append(str(persona_registry.PERSONAS_DIR))
                            except Exception: pass
                            _search_dirs.append(os.path.join(WINGMAN_DIR, "personas"))
                            for _pdir in _search_dirs:
                                ppath = os.path.join(_pdir, f"{pid}.json")
                                if os.path.exists(ppath):
                                    with open(ppath, "r", encoding="utf-8") as _pf:
                                        vid = json.load(_pf).get("voice", {}).get("voice_id")
                                    if vid: break
                        if vid:
                            import base64
                            audio = voice_forge.speak_with_voice(result["text"], vid)
                            if audio:
                                result["audio_b64"] = base64.b64encode(audio).decode()
                                result["audio_format"] = "mp3"
                    except Exception as _ae:
                        pass  # fallback to browser TTS
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/forge/generate":
            # POST /forge/generate {"answers": {"windows": "3", "lost": "a pen", ...}}
            # Calls the LLM to generate a unique first companion from the
            # player's forge answers. Returns {ok, persona: {id, display_name,
            # essence, arrival_text, ...}} and writes the persona JSON to disk.
            try:
                answers = data.get("answers") or {}
                import llm_router
                # Build the generation prompt from answers
                answer_lines = "\n".join(f"  {k}: \"{v}\"" for k, v in answers.items()
                                         if k not in ("steam",) and v)
                system = (
                    "You are the Ember Forge — the cosmic engine that shapes companions "
                    "from the raw material of a player's soul. A player has answered "
                    "questions about themselves. From their answers, you will forge ONE "
                    "unique companion who was shaped by this specific human's texture.\n\n"
                    "Output a single JSON object with EXACTLY these keys:\n"
                    "  id           - lowercase_underscore, 3-15 chars\n"
                    "  display_name - their name as they'd introduce themselves (2-3 words)\n"
                    "  origin       - where/when they're from (brief, evocative)\n"
                    "  era          - time period or 'timeless'\n"
                    "  essence      - 2 sentences capturing who they ARE\n"
                    "  archetype    - one word (guardian, scholar, wanderer, rebel, healer...)\n"
                    "  affinity_tags - array of 4 from: stealth, empathic, cautious, observant, "
                    "combat, direct, dominance, decisive, analytical, technical, perceptive, "
                    "curious, diplomatic, rhetorical, strategic, psionic, commanding, survivor, balanced\n"
                    "  stats        - {str, dex, con, int, wis, cha} each 8-18\n"
                    "  quirks       - array of 3 short personality quirks\n"
                    "  signature_quotes - array of 3 things they'd say\n"
                    "  portrait_art_prompt - a detailed visual description for AI portrait "
                    "generation: physical appearance, expression, lighting, mood. Written "
                    "as an art prompt, not prose. Example: 'weathered woman in her 40s, "
                    "silver-streaked dark hair, warm brown eyes, faint scar across left "
                    "temple, firelight from below, expression of patient amusement, oil "
                    "painting style, warm palette, Rembrandt lighting'\n"
                    "  arrival_text - 2-3 sentences in the Keeper's voice describing the "
                    "moment this person walked into the firelight. Present tense. Reference "
                    "ONE of the player's answers obliquely — don't quote it, echo it.\n\n"
                    "Rules:\n"
                    "- This is NOT a known character. Invent someone new.\n"
                    "- Their personality should be SHAPED by the player's answers, not "
                    "literally derived. 'Rain' as a last sound doesn't mean they love rain — "
                    "it means they understand ambient quiet.\n"
                    "- They should feel like a real person, not a fantasy archetype.\n"
                    "- IMPORTANT: This persona just arrived. They are as NEW to this world "
                    "as the player is. They don't understand the fire, the forge, or why "
                    "they're here. They are curious, slightly disoriented, and discovering "
                    "alongside the player. Their arrival_text should reflect this — not "
                    "confident mastery, but the quiet bewilderment of someone who just "
                    "woke up somewhere they don't recognize and decided to stay.\n"
                    "- LEGENDARY ANSWERS: If the player named a specific video game, book, "
                    "movie, or fictional character, the ember should be SHAPED BY that "
                    "world's aesthetic, themes, and tone. If they said 'Hallownest' (Hollow "
                    "Knight), the ember should feel like someone from that world — insectoid "
                    "undertones, themes of forgotten kingdoms, silence, exploration. If they "
                    "said 'Mass Effect', lean toward command, loyalty, star-travel. Do NOT "
                    "copy a character. Create someone who COULD exist in that universe but "
                    "never did. The reference shapes the flavor, not the identity.\n"
                    "- Return ONLY the JSON, no prose before or after."
                )
                user_msg = f"The player answered:\n{answer_lines}"
                raw = llm_router.call(
                    messages=[{"role": "user", "content": user_msg}],
                    system=system,
                    max_tokens=800,
                    role="keeper",
                )
                # Parse JSON from response
                import re as _re
                json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
                if json_match:
                    persona = json.loads(json_match.group())
                else:
                    raise ValueError("No JSON in LLM response")
                # Sanitize
                pid = (persona.get("id") or "forged_" + str(int(time.time())))[:20]
                pid = _re.sub(r'[^a-z0-9_]', '_', pid.lower())
                persona["id"] = pid
                persona.setdefault("display_name", "A Stranger")
                persona.setdefault("essence", "Someone the forge shaped from your answers.")
                persona.setdefault("status", "camp")
                # Validate affinity_tags against the known set
                _VALID_TAGS = {"stealth","empathic","cautious","observant","combat","direct",
                    "dominance","decisive","analytical","technical","perceptive","curious",
                    "diplomatic","rhetorical","strategic","psionic","commanding","survivor","balanced"}
                raw_tags = persona.get("affinity_tags") or []
                valid_tags = [t for t in raw_tags if t in _VALID_TAGS]
                if len(valid_tags) < 4:
                    # Map unknown tags to closest matches
                    _TAG_MAP = {"water":"cautious","depth":"perceptive","ocean":"observant",
                        "memory":"empathic","time":"analytical","light":"curious",
                        "nature":"observant","shadow":"stealth","force":"combat",
                        "mind":"analytical","spirit":"psionic","word":"rhetorical"}
                    for t in raw_tags:
                        if t not in _VALID_TAGS and len(valid_tags) < 4:
                            mapped = _TAG_MAP.get(t.split()[0] if ' ' in t else t, "balanced")
                            if mapped not in valid_tags:
                                valid_tags.append(mapped)
                    while len(valid_tags) < 4:
                        for fallback in ["observant","curious","cautious","balanced"]:
                            if fallback not in valid_tags:
                                valid_tags.append(fallback); break
                persona["affinity_tags"] = valid_tags[:4]
                persona.setdefault("archetype", "wanderer")
                persona.setdefault("voice", {"engine": "edge_tts", "sample": "", "style_hint": "warm"})
                persona.setdefault("wake_aliases", [persona.get("display_name", "").split()[0].lower()])
                persona.setdefault("persona_prompt", f"You are {persona['display_name']}. {persona.get('essence', '')}")
                persona.setdefault("banter_voice_label", persona.get("display_name", ""))
                persona.setdefault("relationships", {})
                persona.setdefault("knowledge_cutoff", "")
                persona.setdefault("knowledge_domains", [])
                persona.setdefault("ignorance_domains", [])
                persona.setdefault("profile_image", "")
                persona.setdefault("portrait_art_prompt", "")
                # Write to personas/ directory
                # Write to the active slot's personas/ dir (not the shared dir)
                try:
                    import persona_registry as _pr
                    _personas_dir = str(_pr.PERSONAS_DIR)
                except Exception:
                    _personas_dir = os.path.join(WINGMAN_DIR, "personas")
                os.makedirs(_personas_dir, exist_ok=True)
                persona_path = os.path.join(_personas_dir, f"{pid}.json")
                with open(persona_path, "w", encoding="utf-8") as _f:
                    json.dump(persona, _f, indent=2, ensure_ascii=False)
                # Generate a unique ElevenLabs voice if key is available
                try:
                    import voice_forge
                    voice_id = voice_forge.generate_voice(persona)
                    if voice_id:
                        persona["voice"] = {
                            "engine": "elevenlabs",
                            "voice_id": voice_id,
                            "generated": True,
                        }
                        # Re-save with voice_id
                        with open(persona_path, "w", encoding="utf-8") as _f:
                            json.dump(persona, _f, indent=2, ensure_ascii=False)
                except Exception as _ve:
                    print(f"[forge] voice generation skipped: {_ve}")

                # Reload registry so the persona is immediately available
                if _PERSONA_OK:
                    _persona_registry.load()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "persona": persona}).encode())
            except Exception as _e:
                import traceback; traceback.print_exc()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/forge/begin":
            # POST /forge/begin {"starter_id": "shadowheart", "answers": {...}, "player_name": "Eric"}
            # Called after the Ember Forge question sequence. Records the
            # first persona arrival and stores the player's forge answers
            # for the Keeper to reference in future narration.
            try:
                import game_progression
                starter_id = (data.get("starter_id") or "shadowheart").strip()
                answers = data.get("answers") or {}
                player_name = (data.get("player_name") or "you").strip()
                # Record the arrival
                result = game_progression.record_arrival(starter_id)
                # Fire initial gates
                game_progression.check_gates()
                # Store forge answers for the Keeper's future use (birthday,
                # preferences, etc.) in a small file the session briefing reads.
                forge_data = {
                    "starter_id": starter_id,
                    "player_name": player_name,
                    "answers": answers,
                    "ts": time.time(),
                }
                # Save to whichever slot is currently active
                try:
                    import game_progression
                    forge_dir = str(game_progression.STORE_FILE.parent)
                except Exception:
                    forge_dir = WINGMAN_DIR
                forge_path = os.path.join(forge_dir, ".forge_answers.json")
                with open(forge_path, "w", encoding="utf-8") as _f:
                    json.dump(forge_data, _f, indent=2, ensure_ascii=False)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ok": True,
                    "starter_id": starter_id,
                    "library_size": result.get("library_size", 1),
                }).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/reward/draw":
            # POST /reward/draw {} — draw 3 reward cards
            try:
                import reward_cards
                cards = reward_cards.draw_cards(3)
                crystals = reward_cards.get_crystals()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "cards": cards, "crystals": crystals}).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/reward/pick":
            # POST /reward/pick {"card_id": "patch_up"}
            try:
                import reward_cards
                card_id = (data.get("card_id") or "").strip()
                if not card_id:
                    raise ValueError("missing card_id")
                result = reward_cards.pick_card(card_id)
                if result.get("ok"):
                    # Apply card effects server-side
                    card = result.get("card") or {}
                    eff = card.get("effect") or {}
                    _apply_reward_card_effect(eff)
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/reward/skip":
            # POST /reward/skip {}
            try:
                import reward_cards
                result = reward_cards.skip_cards()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/party/memento/equip":
            # POST /party/memento/equip {"persona_id": "shadowheart", "memento_id": "abc123", "slot": 0}
            try:
                import memento_store
                pid = (data.get("persona_id") or "").strip()
                mid = (data.get("memento_id") or data.get("id") or "").strip()
                slot = int(data.get("slot", -1))
                if not pid or not mid:
                    raise ValueError("missing persona_id or memento_id")
                result = memento_store.equip_memento(pid, mid, slot)
                self.send_response(200 if result.get("ok") else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/party/memento/unequip":
            # POST /party/memento/unequip {"persona_id": "shadowheart", "memento_id": "abc123"}
            # OR {"persona_id": "shadowheart", "slot": 2}
            try:
                import memento_store
                pid = (data.get("persona_id") or "").strip()
                mid = (data.get("memento_id") or data.get("id") or "").strip()
                slot = data.get("slot")
                if not pid:
                    raise ValueError("missing persona_id")
                if mid:
                    result = memento_store.unequip_memento(pid, mid)
                elif slot is not None:
                    result = memento_store.unequip_slot(pid, int(slot))
                else:
                    raise ValueError("provide memento_id or slot")
                self.send_response(200 if result.get("ok") else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/start":
            # POST /session/start {"player_name": "Eric", "party": [...], "scenario_hint": "?"}
            # Creates a new Keeper session and returns the opening narration.
            # party defaults to the current in-party personas if omitted.
            player_name = (data.get("player_name") or "Eric").strip()
            party_ids = data.get("party")
            if not party_ids:
                # Default: everyone currently in the party
                if _PERSONA_OK:
                    _persona_registry.load()
                    party_ids = [p.id for p in _persona_registry.party()]
                else:
                    party_ids = []
            scenario_hint = (data.get("scenario_hint") or "").strip()
            try:
                import session_forge
                result = session_forge.start_session(player_name, party_ids, scenario_hint)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, **result}, default=str).encode())
                log.info(f"[session/start] {player_name} + {party_ids} -> {len(result.get('narration', ''))} chars")
            except Exception as _e:
                log.error(f"[session/start] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/begin_encounter":
            # POST /session/begin_encounter {}
            # Commits the turn order and flips the session from planning to
            # active. After this, advance_turn starts producing per-turn beats.
            try:
                import session_forge
                result = session_forge.begin_encounter()
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())
                log.info(f"[session/begin_encounter] next_turn={result.get('next_turn')}")
            except Exception as _e:
                log.error(f"[session/begin_encounter] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/reorder":
            # POST /session/reorder {"order": ["tesla", "player", "laezel", ...]}
            # Player-initiated turn-order change during the planning phase.
            # Same entries, different positions.
            new_order = data.get("order") or []
            if not isinstance(new_order, list):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": false, "error": "order must be an array"}')
                return
            try:
                import session_forge
                result = session_forge.reorder_turns(new_order)
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())
            except Exception as _e:
                log.error(f"[session/reorder] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/advance_turn":
            # POST /session/advance_turn {}
            # Pulls the next turn in the round. If it's a persona, generates
            # their per-turn beat via Sonnet. If it's the player, returns a
            # waiting-for-player signal with a deadline timestamp. If the
            # round is complete, generates the resolution beat and archives.
            try:
                import session_forge
                result = session_forge.advance_turn()
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())
                log.info(f"[session/advance_turn] who={result.get('who')} next={result.get('next_turn')}")
            except Exception as _e:
                log.error(f"[session/advance_turn] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/submit_turn":
            # POST /session/submit_turn {"text": "..."}
            # Player-submitted turn. Only valid when it's the player's turn.
            text = (data.get("text") or "").strip()
            if not text:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": false, "error": "text is required"}')
                return
            try:
                import session_forge
                result = session_forge.submit_player_turn(text)
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())
                log.info(f"[session/submit_turn] {len(text)} chars, next={result.get('next_turn')}")
            except Exception as _e:
                log.error(f"[session/submit_turn] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/interrupt":
            # POST /session/interrupt {}
            # Player hits the INTERRUPT button - move 'player' to next in order.
            try:
                import session_forge
                result = session_forge.interrupt()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())
                log.info(f"[session/interrupt] next={result.get('next_turn')}")
            except Exception as _e:
                log.error(f"[session/interrupt] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/skip_player":
            # POST /session/skip_player {}
            # The 30-second player turn expired without input.
            try:
                import session_forge
                result = session_forge.skip_player_turn()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())
                log.info(f"[session/skip_player] next={result.get('next_turn')}")
            except Exception as _e:
                log.error(f"[session/skip_player] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/turn":
            # POST /session/turn {"text": "..."}  - player's turn.
            # Returns the next Keeper narration.
            text = (data.get("text") or "").strip()
            if not text:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": false, "error": "text is required"}')
                return
            try:
                import session_forge
                result = session_forge.take_turn(text)
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())
                log.info(f"[session/turn] {len(text)} chars in -> {len(result.get('narration', ''))} chars out")
            except Exception as _e:
                log.error(f"[session/turn] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/session/end":
            # POST /session/end {}  - player-initiated wrap-up.
            # The Keeper writes a closing paragraph and emits the memento note.
            try:
                import session_forge
                result = session_forge.end_session_now()
                status = 200 if result.get("ok") else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())
                log.info(f"[session/end] archived")
            except Exception as _e:
                log.error(f"[session/end] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/keeper/ritual":
            # POST /keeper/ritual {"id": "<persona_id>", "kind": "speak"}
            # Void-vector story moment. The Keeper narrates a short in-character
            # beat about a specific persona - what she sees in them right now,
            # informed by their inner weight, current stress, camp vignettes,
            # and recent relationships. Returns narration text (which the
            # client will voice via /keeper/speak) and an optional memento.
            pid = (data.get("id") or "").strip()
            kind = (data.get("kind") or "speak").strip()
            if kind != "speak":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": f"ritual kind '{kind}' not implemented"}).encode())
                return
            try:
                if not _PERSONA_OK:
                    raise RuntimeError("persona registry unavailable")
                _persona_registry.load()
                _p = _persona_registry.get(pid)
                if not _p:
                    raise RuntimeError(f"unknown persona {pid}")

                # Build context for the Keeper
                _inner_weight = ""
                try:
                    import session_forge as _sf
                    _inner_weight = _sf._INNER_WEIGHT_BRIEFS.get(pid, "")
                except Exception:
                    pass
                _stress_level = 0
                _affliction = None
                _camp_flavors = []
                try:
                    import stress_store
                    _stress_level = stress_store.get_stress(pid)
                    _affliction = stress_store.get_affliction(pid)
                    _events = stress_store.recent_events_for(pid, n=3)
                    _camp_flavors = [e.get("flavor") for e in _events if e.get("flavor")]
                except Exception:
                    pass

                # Compose the ritual prompt
                _system = (
                    "You are The Keeper, the eternal game master and forge-master of "
                    "the void. A player has brought a specific companion to the edge "
                    "of your forge and asked you to SPEAK of them - to name what you "
                    "see in them right now. This is not a session. This is a quiet "
                    "ritual moment, just between you and the player.\n\n"
                    "Speak as The Keeper, in first person. One or two short paragraphs. "
                    "Address the player directly. Reference the specific weight this "
                    "persona is carrying right now - their stress, their recent rest "
                    "beats, their inner weight - without explaining mechanics. Show "
                    "what you see. Be warm, specific, and slightly dangerous in the "
                    "way only someone who has watched everything can be. Do not write "
                    "dialogue from the persona themselves; only your own observation.\n\n"
                    "Keep it under 150 words. End with a single sentence that names "
                    "the shape of a memento this moment could become if crystallized."
                )
                _user = (
                    f"COMPANION: {_p.display_name} ({_p.id})\n"
                    f"  Essence: {(_p.essence or '')[:280]}\n"
                    f"\nINNER WEIGHT:\n{_inner_weight or '(unknown)'}\n"
                    f"\nCURRENT STRESS: {_stress_level} / 20\n"
                )
                if _affliction:
                    _user += f"CURRENT AFFLICTION: {_affliction}\n"
                if _camp_flavors:
                    _user += "\nRECENT CAMP BEATS:\n"
                    for f in _camp_flavors:
                        _user += f'  - "{f}"\n'
                _user += (
                    f"\nThe player has brought {_p.display_name} to the edge of your "
                    f"forge and asked you to speak of them. Speak."
                )

                # Call Claude Sonnet
                _api_key = get_env("ANTHROPIC_API_KEY")
                if not _api_key:
                    raise RuntimeError("ANTHROPIC_API_KEY not configured")
                _body = json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 500,
                    "system": _system,
                    "messages": [{"role": "user", "content": _user}],
                }).encode()
                _req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=_body,
                    headers={
                        "x-api-key": _api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                )
                with urllib.request.urlopen(_req, timeout=60) as _rr:
                    _resp = json.loads(_rr.read())
                _narration = "".join(
                    block.get("text", "")
                    for block in _resp.get("content", [])
                    if block.get("type") == "text"
                ).strip()

                log.info(f"[keeper/ritual] speak on {pid} -> {len(_narration)} chars")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ok":        True,
                    "narration": _narration,
                    "persona":   {"id": _p.id, "display_name": _p.display_name},
                    # Memento forging from Keeper rituals is a follow-up
                    "memento":   None,
                }).encode())
            except Exception as _e:
                log.error(f"[keeper/ritual] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/keeper/speak":
            # POST /keeper/speak {"text": "...", "voice_id": "optional-override"}
            # Calls the ElevenLabs TTS API via el_voice_sampler.el_tts() and
            # streams the raw MP3 bytes back as the response body. The browser
            # wraps the blob in an <audio> element and plays it inline.
            # This is The Keeper's voice layer - she speaks through this route.
            text = (data.get("text") or "").strip()
            if not text:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": false, "error": "text is required"}')
                return
            voice_id = (data.get("voice_id") or "").strip() or get_env("KEEPER_VOICE_ID")
            if not voice_id:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": false, "error": "KEEPER_VOICE_ID not configured"}')
                return
            try:
                import el_voice_sampler as _els
                mp3_bytes = _els.el_tts(voice_id, text)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(mp3_bytes)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(mp3_bytes)
                log.info(f"[keeper/speak] {len(text)} chars -> {len(mp3_bytes)} mp3 bytes")
            except Exception as _e:
                log.error(f"[keeper/speak] failed: {_e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(_e)}).encode())
            return

        elif self.path == "/party/mute":
            # Body: {"id": "<persona_id>", "muted": bool}
            # Toggles or sets the muted flag for a persona. Persisted to
            # .party_muted.json. The flag is included in /party/state so the
            # widget + studio UI can render it; wiring it into the actual TTS
            # pipeline (skip speaking for muted members) is a follow-up.
            _pid = (data.get("id") or "").strip()
            _want_muted = bool(data.get("muted"))
            resp = {"ok": False}
            if _pid:
                with _mute_lock:
                    if _want_muted:
                        _muted_personas.add(_pid)
                    else:
                        _muted_personas.discard(_pid)
                    _save_muted(_muted_personas)
                resp = {"ok": True, "id": _pid, "muted": _want_muted}
            else:
                resp = {"ok": False, "error": "missing id"}
            self.send_response(200 if resp.get("ok") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            return

        elif self.path == "/party/widget/kill":
            # Best-effort kill of any running party_widget_launcher processes.
            # Uses wmic on Windows to match on the command line so we don't
            # take out unrelated python processes.
            resp = {"ok": False, "killed": 0}
            try:
                import subprocess
                if sys.platform == "win32":
                    # List python.exe pids whose command line contains party_widget_launcher
                    _cp = subprocess.run(
                        ["wmic", "process", "where",
                         "name='python.exe' and CommandLine like '%%party_widget_launcher%%'",
                         "get", "ProcessId"],
                        capture_output=True, text=True, timeout=5,
                    )
                    _pids = []
                    for _line in _cp.stdout.splitlines():
                        _line = _line.strip()
                        if _line.isdigit():
                            _pids.append(int(_line))
                    for _pid in _pids:
                        try:
                            subprocess.run(["taskkill", "/F", "/PID", str(_pid), "/T"],
                                           capture_output=True, timeout=5)
                        except Exception:
                            pass
                    resp = {"ok": True, "killed": len(_pids), "pids": _pids}
                else:
                    # POSIX fallback: pkill by script name
                    subprocess.run(["pkill", "-f", "party_widget_launcher"],
                                   capture_output=True, timeout=5)
                    resp = {"ok": True}
            except Exception as _e:
                resp = {"ok": False, "error": str(_e)}
            self.send_response(200 if resp.get("ok") else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            return

        elif self.path == "/party/widget/launch":
            # Body: {} or {"width": N, "height": N, "x": N, "y": N}
            # Spawns party_widget_launcher.py as a detached process so the
            # widget lives independently from the studio server.
            resp = {"ok": False}
            try:
                import subprocess
                _launcher = os.path.join(WINGMAN_DIR, "party_widget_launcher.py")
                if not os.path.exists(_launcher):
                    raise RuntimeError("party_widget_launcher.py not found")
                _args = [sys.executable, _launcher]
                if data.get("width"):  _args += ["--width",  str(int(data["width"]))]
                if data.get("height"): _args += ["--height", str(int(data["height"]))]
                if data.get("x") is not None: _args += ["--x", str(int(data["x"]))]
                if data.get("y") is not None: _args += ["--y", str(int(data["y"]))]
                # Spawn the widget as a GUI child process. DETACHED_PROCESS
                # (0x08) is wrong for GUI children on Windows because it
                # removes the console association in a way that kills
                # pywebview's EdgeChromium host. Use CREATE_NO_WINDOW
                # (0x08000000) + CREATE_NEW_PROCESS_GROUP (0x0200) so the
                # widget has no console but can still render a window and
                # outlives studio_server if it restarts. We also redirect
                # stdout/stderr to a rotating log so the next time it
                # crashes we can actually see why.
                _log_path = os.path.join(WINGMAN_DIR, "party_widget_launcher.log")
                _log_fh = open(_log_path, "a", encoding="utf-8")
                _log_fh.write(f"\n=== launch {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                _log_fh.flush()
                _kwargs = {
                    "cwd": WINGMAN_DIR,
                    "close_fds": True,
                    "stdout": _log_fh,
                    "stderr": _log_fh,
                    "stdin":  subprocess.DEVNULL,
                }
                if sys.platform == "win32":
                    CREATE_NO_WINDOW = 0x08000000
                    CREATE_NEW_PG    = 0x00000200
                    _kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PG
                _proc = subprocess.Popen(_args, **_kwargs)
                resp = {"ok": True, "pid": _proc.pid}
            except Exception as _e:
                resp = {"ok": False, "error": str(_e)}
            self.send_response(200 if resp.get("ok") else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            return

        elif self.path == "/party/rollcall":
            # Body: {} — triggers a party roll call: every in-party persona
            # speaks one short self-identification line in their own voice.
            # Room_agent watcher picks up via .party_rollcall_request.json.
            resp = {"ok": False}
            try:
                _trig_path = os.path.join(WINGMAN_DIR, ".party_rollcall_request.json")
                with open(_trig_path, "w", encoding="utf-8") as _tf:
                    json.dump({"ts": time.time()}, _tf)
                resp = {"ok": True}
            except Exception as _e:
                resp = {"ok": False, "error": str(_e)}
            self.send_response(200 if resp.get("ok") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
            return

        elif self.path == "/session_costs":
            # Update per-session-mode cost config
            # Expects: {"mode": "meeting", "cost_override": "premium", "model": "...", "max_tokens": N}
            mode = data.pop("mode", None)
            if mode and _COST_OK:
                ok = _cost.set_session_cost_override(mode, **data)
                resp = json.dumps({"ok": ok, "session_costs": _cost.get_session_costs_summary()}).encode()
            else:
                resp = json.dumps({"ok": False, "error": "missing mode or cost_router not loaded"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp)
            return

        elif self.path == "/task":
            # Receive a task from any source (watcher, room_agent, etc.)
            # Writes to tasks/ folder and broadcasts via SSE
            import hashlib as _hl
            task_id = data.get("id") or f"task_{int(time.time())}_{_hl.md5(json.dumps(data).encode()).hexdigest()[:8]}"
            data["id"] = task_id
            data.setdefault("created", datetime.now().isoformat())
            data.setdefault("status", "pending")
            tasks_dir = os.path.join(WINGMAN_DIR, "tasks")
            os.makedirs(tasks_dir, exist_ok=True)
            task_file = os.path.join(tasks_dir, f"{task_id}.json")
            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.info(f"[Task] Created: {task_id} â€” {data.get('title', 'untitled')}")
            broadcast("new_task", data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "task_id": task_id}).encode())
            return

        elif self.path == "/clear":
            conversation.clear()
            broadcast("clear", {})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')


        elif self.path == "/health":
            # Accept health/biometric data from Galaxy Watch relay
            # Expected: {"sleep_state": "awake|light|deep|rem", "heart_rate": 72, "source": "tasker|gadgetbridge"}
            try:
                data = json.loads(body) if body else {}
                from datetime import datetime as _dt
                with _health_lock:
                    if "sleep_state" in data:
                        _health_data["sleep_state"] = data["sleep_state"]
                    if "heart_rate" in data:
                        _health_data["heart_rate"] = int(data["heart_rate"])
                    if "source" in data:
                        _health_data["source"] = data["source"]
                    _health_data["updated_at"] = _dt.now().isoformat()
                broadcast("health_update", dict(_health_data))
                log.info(f"Health update: {_health_data['sleep_state']} HR={_health_data['heart_rate']} from={_health_data['source']}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
            except Exception as e:
                log.error(f"Health POST error: {e}")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        elif self.path == "/agent_state":
            try:
                from datetime import datetime as _dt
                with _agent_state_lock:
                    if "state" in data:
                        _agent_state["state"] = data["state"]
                    _agent_state["updated_at"] = _dt.now().isoformat()
                broadcast("state_change", {"state": data.get("state", "unknown")})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                log.error(f"Agent state POST error: {e}")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        elif self.path == "/sms/incoming":
            # Twilio SMS webhook — incoming text messages
            try:
                # Twilio sends form-encoded, but we may also get JSON
                from_num = data.get("From", "")
                body_txt = data.get("Body", "")
                media = [data[k] for k in sorted(data.keys()) if k.startswith("MediaUrl")]
                log.info(f"SMS incoming from {from_num}: {body_txt[:60]}")
                from social_agent import handle_incoming_sms
                reply = handle_incoming_sms(from_num, body_txt, media)
                broadcast("sms_incoming", {"from": from_num, "body": body_txt, "reply": reply})
                # Return TwiML response (empty — we send replies via API, not TwiML)
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.end_headers()
                self.wfile.write(b'<?xml version="1.0"?><Response></Response>')
            except Exception as e:
                log.error(f"SMS incoming error: {e}")
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.end_headers()
                self.wfile.write(b'<?xml version="1.0"?><Response></Response>')

        elif self.path == "/adventure":
            # Trigger adventure mode
            try:
                utterance = data.get("utterance", data.get("vibe", "somewhere fun"))
                lat = float(data.get("lat", 34.0522))
                lng = float(data.get("lng", -118.2437))
                surprise = data.get("surprise", True)
                log.info(f"Adventure mode: '{utterance}' at ({lat},{lng})")
                from adventure_agent import adventure
                result = threading.Thread(
                    target=lambda: _run_adventure(utterance, lat, lng, surprise),
                    daemon=True
                ).start()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "status": "searching"}).encode())
            except Exception as e:
                log.error(f"Adventure error: {e}")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def _run_adventure(utterance, lat, lng, surprise):
    """Run adventure pipeline in background thread."""
    try:
        from adventure_agent import adventure
        result = adventure(utterance, lat, lng, surprise)
        if result and result.get("destination"):
            broadcast("adventure_result", result)
            log.info(f"Adventure found: {result['destination'].get('name', '?')}")
        else:
            broadcast("adventure_result", {"error": "No options found", "message": result.get("message", "")})
    except Exception as e:
        log.error(f"Adventure pipeline error: {e}")
        broadcast("adventure_result", {"error": str(e)})


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True



# â”€â”€ Task folder watcher â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
TASKS_DIR = os.path.join(WINGMAN_DIR, "tasks")
TASKS_DONE_DIR = os.path.join(WINGMAN_DIR, "tasks", "done")

def get_live_tasks() -> list:
    """Read all pending tasks from tasks/ folder (excluding README and done/)."""
    tasks = []
    try:
        for fname in sorted(os.listdir(TASKS_DIR)):
            if fname in ("README.json",) or fname.startswith("."):
                continue
            fpath = os.path.join(TASKS_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                data = json.loads(open(fpath, encoding="utf-8").read())
                tasks.append({
                    "id":     fname,
                    "task":   data.get("task", fname),
                    "source": data.get("source", "?"),
                    "tier":   data.get("tier", "?"),
                    "status": "pending",
                    "mtime":  os.path.getmtime(fpath),
                })
            except Exception:
                tasks.append({"id": fname, "task": fname, "status": "pending"})
    except Exception:
        pass
    return tasks


def watch_tasks_folder():
    """Poll tasks/ folder every 3s, broadcast SSE when contents change."""
    last_snapshot = None
    while True:
        try:
            current = {f: os.path.getmtime(os.path.join(TASKS_DIR, f))
                       for f in os.listdir(TASKS_DIR)
                       if f not in ("README.json",) and
                       os.path.isfile(os.path.join(TASKS_DIR, f))}
            if current != last_snapshot:
                last_snapshot = current
                tasks = get_live_tasks()
                broadcast("tasks_update", {"tasks": tasks})
                log.debug(f"Tasks update: {len(tasks)} pending")
        except Exception as e:
            log.debug(f"Task watcher error: {e}")
        time.sleep(3)


def run_server():
    # Check if port already in use â€” enforce single instance
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", PORT)) == 0:
            log.error(f"Port {PORT} already in use â€” studio already running. Exiting.")
            sys.exit(1)
    # Start task folder watcher
    threading.Thread(target=watch_tasks_folder, daemon=True).start()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), StudioHandler)
    log.info(f"Amplify Studio running at http://localhost:{PORT}")
    try:
        import boot_log as _bl
        _bl.stamp("studio", "READY", f"http://localhost:{PORT}")
    except Exception:
        pass
    server.serve_forever()


if __name__ == "__main__":
    add_turn("system", "Amplify Studio started", "startup")
    run_server()
