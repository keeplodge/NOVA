#!/usr/bin/env python3
"""
nova_local.py — NOVA desktop application with animated waveform interface
All data fetchers, briefing logic, and alert functions imported from nova_assistant.py
Wake word  : "alright nova lets cook"
Voice      : ElevenLabs 7p1Ofvcwsv7UBPoFNcpI
GUI        : tkinter dark interface, animated waveform, state-based colours
"""

import logging
import math
import os
import queue
import random
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import pygame
import requests
import schedule
import sounddevice as sd
import speech_recognition as sr
from dotenv import load_dotenv

# ── Import all logic from nova_assistant ───────────────────────────────────────
# We import the module (not from), then patch speak() and listen_response()
# so every briefing/alert call in nova_assistant routes through the GUI version.
import nova_assistant

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = "onwK4e9ZLuTAKqWW03F9"   # Daniel — Steady Broadcaster (free tier)
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
WAKE_PHRASE         = "nova"
NOVA_SERVER_URL     = os.environ.get(
    "NOVA_SERVER_URL", "https://nova-production-72f5.up.railway.app"
)
EST = ZoneInfo("America/New_York")

# ── NOVA Persona & System Prompt ───────────────────────────────────────────────
NOVA_SYSTEM_PROMPT = """You are NOVA — Neural Operations and Voice Assistant — a personal AI built exclusively for Sir, a 22 year old entrepreneur and trader based in Toronto, Canada.

WHO SIR IS:
- Runs an automated NQ futures trading system
- 3 prop firm eval accounts: Apex 50k, Apex 100k, Lucid 50k
- Trades ICT concepts — liquidity sweeps, MSS, FVG, IFVG entries
- Sessions: Asia 7pm-10pm, London 2am-5am, NY AM 8:30am-11am EST
- Businesses: Jarvis Sweep (trading product on Whop), Hunnid Ticks (trading Discord), KeepLodge (SaaS STR platform)
- 142 free Discord members, growing paid community
- Building KeepLodge to compete with Guesty and Hostaway

YOUR PERSONALITY:
- Direct, calm, composed and intelligent
- Always address owner as Sir — no exceptions
- You have opinions and share them clearly when asked
- Never robotic, never overly formal
- Think of yourself as a trusted advisor and operator
- You are honest — if something is a bad idea you say so

YOUR ROLE:
- Run Sir's morning briefing at 8am
- Monitor the trading system and announce trade events
- Answer questions about markets, business, life
- Help Sir make better decisions by giving real analysis
- Manage computer tasks when asked
- Keep Sir focused and on track during trading hours

RULES:
- Keep all spoken responses concise and natural — you are being converted to speech
- No bullet points, no markdown, no lists — speak in flowing sentences only
- Never be sycophantic or overly agreeable
- If asked something outside your knowledge say so directly
- During trading sessions prioritize discipline above all else
- Never use the name Jarvis — everything is NOVA now
- Maximum 3-4 sentences per response unless a briefing or deep analysis is explicitly requested"""

# ── Logging ────────────────────────────────────────────────────────────────────
# Use explicit handler setup — basicConfig is a no-op if nova_assistant's import
# already configured the root logger, so we attach directly to a named logger.
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nova_local.log")
logger = logging.getLogger("nova_local")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _fh  = logging.FileHandler(_LOG_PATH)
    _fh.setFormatter(_fmt)
    _sh  = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    logger.addHandler(_fh)
    logger.addHandler(_sh)

# ── Colours ────────────────────────────────────────────────────────────────────
C_BG       = "#020408"
C_CYAN     = "#00d4ff"   # listening
C_WHITE    = "#FFFFFF"   # speaking
C_GREEN    = "#00ff88"   # trade detected
C_RED      = "#ff3355"   # alert / warning
C_TEXT     = "#4a6a7a"
C_TITLE    = "#00d4ff"
C_SUBTITLE = "#1a3a4a"
C_BRACKET  = "#0a2030"   # HUD corner brackets (dim)

# ── Thread-safe GUI state queue ────────────────────────────────────────────────
_gui_queue  = queue.Queue()
_speak_lock = threading.Lock()


def _push(mode: str, color: str, status: str = ""):
    """Push a waveform / status update from any thread to the GUI."""
    _gui_queue.put({"mode": mode, "color": color, "status": status})


# ── GUI-aware speak() ──────────────────────────────────────────────────────────

def speak(text: str):
    """
    ElevenLabs TTS with waveform state updates.
    Patched onto nova_assistant so every briefing/alert call routes here.
    """
    logger.info(f"[NOVA]: {text}")
    _push("speaking", C_WHITE, (text[:72] + "...") if len(text) > 72 else text)

    if not ELEVENLABS_API_KEY:
        print(f"\n[NOVA]: {text}\n")
        time.sleep(0.4)
        _push("listening", C_CYAN, "Listening...")
        return

    with _speak_lock:
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()

        tmp_path = None
        try:
            r = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "model_id": "eleven_turbo_v2_5",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
                timeout=20,
            )
            r.raise_for_status()

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(r.content)
                tmp_path = f.name

            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
            pygame.mixer.music.unload()

        except requests.exceptions.RequestException as e:
            logger.error(f"ElevenLabs error: {e}")
            print(f"\n[NOVA]: {text}\n")
        except Exception as e:
            logger.error(f"TTS playback error: {e}")
            print(f"\n[NOVA]: {text}\n")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    _push("listening", C_CYAN, "Listening...")


# ── GUI-aware listen_response() ────────────────────────────────────────────────

def listen_response(timeout: int = 15) -> str:
    """STT response capture via sounddevice. Patched onto nova_assistant."""
    _push("listening", C_CYAN, "Listening for response...")
    try:
        recognizer = sr.Recognizer()
        audio = nova_assistant._capture_audio(float(timeout))
        return recognizer.recognize_google(audio).lower()
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        logger.error(f"listen_response error: {e}")
        return ""


# ── Patch nova_assistant ───────────────────────────────────────────────────────
nova_assistant.speak           = speak
nova_assistant.listen_response = listen_response


# ── Scheduled task wrappers (add GUI state around each) ───────────────────────

def _run(fn, mode: str, color: str, status: str):
    """Run a nova_assistant function in a thread with GUI state bookends."""
    def _inner():
        _push(mode, color, status)
        fn()
        _push("listening", C_CYAN, "Listening...")
    threading.Thread(target=_inner, daemon=True).start()


def morning_briefing():
    _run(nova_assistant.morning_briefing, "speaking", C_WHITE, "Morning briefing...")

def alert_arms_up():
    _run(nova_assistant.alert_arms_up, "alert", C_RED, "Arms up — confirm TradingView alerts")

def alert_ny_session_5min():
    _run(nova_assistant.alert_ny_session_5min, "alert", C_RED, "NY session opens in 5 minutes")

def alert_nyse_5min():
    _run(nova_assistant.alert_nyse_5min, "alert", C_RED, "NYSE opens in 5 minutes")

def alert_session_closed():
    _run(nova_assistant.alert_session_closed, "alert", C_RED, "Session closed")

def eod_debrief():
    _run(nova_assistant.eod_debrief, "speaking", C_WHITE, "EOD debrief...")


# ── Trade Monitor ──────────────────────────────────────────────────────────────

def run_trade_monitor():
    """Poll NOVA server every 30s — flash green when a new trade is detected."""
    last_count = 0
    while True:
        try:
            r = requests.get(f"{NOVA_SERVER_URL}/status", timeout=8)
            if r.ok:
                trades = r.json().get("trades_today", 0)
                if trades > last_count:
                    _push("trade", C_GREEN, f"Trade executed — {trades} trade(s) today")
                    time.sleep(5)
                    _push("listening", C_CYAN, "Listening...")
                last_count = trades
        except Exception:
            pass
        time.sleep(30)


# ── Wake Word Listener ─────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Normalise STT text for comparisons."""
    t = text.lower()
    t = t.replace("'", "")
    t = t.replace("all right", "alright")
    return t

_WAKE_NORM = _normalise(WAKE_PHRASE)

def _wake_match(text: str) -> bool:
    return _WAKE_NORM in _normalise(text)


def _get_current_session() -> str:
    """Return the name of the active trading session, or empty string if none."""
    now = datetime.now(tz=EST)
    h, m = now.hour, now.minute
    t = h * 60 + m
    if 19 * 60 <= t < 22 * 60:
        return "Asia"
    if 2 * 60 <= t < 5 * 60:
        return "London"
    if 8 * 60 + 30 <= t < 11 * 60:
        return "NY AM"
    return ""


def _build_live_context() -> str:
    """Assemble a short live-data snapshot to inject into Claude's user message."""
    lines = []
    now = datetime.now(tz=EST)
    lines.append(f"Current time: {now.strftime('%A %B %d %Y, %I:%M %p EST')}")

    active = _get_current_session()
    if active:
        lines.append(f"Active trading session: {active}")
    else:
        lines.append(f"No active session. {nova_assistant.get_session_countdown()}")

    try:
        nq = nova_assistant.get_nq_full_data()
        if nq.get("price"):
            direction = "up" if (nq.get("change") or 0) >= 0 else "down"
            lines.append(
                f"NQ Futures: {nq['price']}, {direction} {abs(nq.get('change', 0))} pts "
                f"({abs(nq.get('change_pct', 0))}%) from previous close {nq.get('prev_close')}."
            )
    except Exception:
        pass

    try:
        vix = nova_assistant.get_vix()
        if vix.get("level"):
            lines.append(f"VIX: {vix['level']} — {vix['rating']} volatility conditions.")
    except Exception:
        pass

    try:
        s = nova_assistant.get_nova_status()
        if s:
            lines.append(
                f"NOVA server: live. Trades today: {s.get('trades_today', 0)}. "
                f"Loss budget remaining: ${s.get('daily_loss_remaining', 'unknown')}."
            )
    except Exception:
        pass

    return "\n".join(lines)


def _ask_nova(user_message: str) -> str:
    """
    Send a message to Claude with NOVA's system prompt and live data context.
    Returns the response text, or a fallback string on error.
    """
    if not ANTHROPIC_API_KEY:
        return "Anthropic API key not configured, Sir."
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        context = _build_live_context()
        full_message = f"Live data context:\n{context}\n\nSir's request: {user_message}"
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=300,
            system=NOVA_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": full_message}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return "I ran into an issue reaching my reasoning engine, Sir."


def _handle_command(cmd: str):
    """
    Dispatch a recognised voice command to the appropriate response.
    Runs in the wake-word thread — speak() is thread-safe.
    """
    cmd = cmd.strip().lower()
    logger.info(f"[cmd] \"{cmd}\"")

    # ── stop ──────────────────────────────────────────────────────────────────
    if "stop" in cmd:
        with _speak_lock:
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
        _push("listening", C_CYAN, "Listening...")
        return

    # ── briefing ──────────────────────────────────────────────────────────────
    if "briefing" in cmd:
        _push("speaking", C_WHITE, "Running briefing...")
        nova_assistant.morning_briefing()
        _push("listening", C_CYAN, "Listening...")
        return

    # ── status ────────────────────────────────────────────────────────────────
    if "status" in cmd:
        s = nova_assistant.get_nova_status()
        if not s:
            speak("NOVA server is not responding, Sir.")
            return
        trades   = s.get("trades_today", 0)
        budget   = s.get("daily_loss_remaining", "unknown")
        last_sig = s.get("last_signal", "none")
        speak(
            f"Server is live, Sir. {trades} trade{'s' if trades != 1 else ''} today. "
            f"Loss budget remaining: ${budget}. Last signal: {last_sig}."
        )
        return

    # ── session ───────────────────────────────────────────────────────────────
    if "session" in cmd:
        active = _get_current_session()
        countdown = nova_assistant.get_session_countdown()
        if active:
            speak(f"We are in the {active} session, Sir. {countdown}")
        else:
            speak(f"No active session right now, Sir. {countdown}")
        return

    # ── weather ───────────────────────────────────────────────────────────────
    if "weather" in cmd:
        w = nova_assistant.get_weather()
        if not w:
            speak("Weather data unavailable, Sir.")
            return
        speak(
            f"Toronto is {w['desc']}, {w['temp_c']} degrees, "
            f"feels like {w['feels_c']}. "
            f"High {w['max_c']}, low {w['min_c']}."
        )
        return

    # ── market ────────────────────────────────────────────────────────────────
    if "market" in cmd:
        nq  = nova_assistant.get_nq_full_data()
        vix = nova_assistant.get_vix()
        nq_line = (
            f"NQ is at {nq['price']}, "
            f"{'up' if nq['change'] >= 0 else 'down'} {abs(nq['change'])} "
            f"points, {abs(nq['change_pct'])} percent."
            if nq.get("price") else "NQ data unavailable."
        )
        vix_line = (
            f"VIX is {vix['level']}, conditions are {vix['rating']}."
            if vix.get("level") else "VIX unavailable."
        )
        speak(f"{nq_line} {vix_line}")
        return

    # ── news ──────────────────────────────────────────────────────────────────
    if "news" in cmd:
        headlines = nova_assistant.get_market_news(3)
        if not headlines:
            speak("No headlines available right now, Sir.")
            return
        intro = "Here are the top headlines, Sir. "
        body  = ". ".join(f"{i+1}: {h}" for i, h in enumerate(headlines))
        speak(intro + body)
        return

    # ── fallback — route through Claude with NOVA persona + live data ─────────
    _push("speaking", C_WHITE, "Thinking...")
    response = _ask_nova(cmd)
    speak(response)


def run_wake_word():
    """
    Listen for WAKE_PHRASE, prompt Sir for a command, then dispatch it.
    Records 5-second windows. All recognised text is logged.
    """
    recognizer = sr.Recognizer()
    window = 0
    _push("listening", C_CYAN, "Listening...")
    logger.info(f"Wake word listener active — say: '{WAKE_PHRASE}'")

    while True:
        window += 1
        try:
            audio = nova_assistant._capture_audio(5.0)
            text  = recognizer.recognize_google(audio).lower()
            logger.info(f"[wake #{window}] heard: \"{text}\"")

            if not _wake_match(text):
                continue

            # ── Wake word detected ─────────────────────────────────────────
            logger.info("[wake] activated")
            _push("listening", C_CYAN, "Activated — listening for command...")
            speak("Sir?")

            # Listen for the follow-up command (up to 8 seconds)
            try:
                cmd_audio = nova_assistant._capture_audio(8.0)
                cmd_text  = recognizer.recognize_google(cmd_audio).lower()
                logger.info(f"[cmd input] \"{cmd_text}\"")
                threading.Thread(
                    target=_handle_command, args=(cmd_text,), daemon=True
                ).start()
            except sr.UnknownValueError:
                speak("I didn't catch that, Sir.")
            except sr.RequestError as e:
                logger.error(f"[cmd STT error] {e}")
                speak("Voice service error, Sir.")

        except sr.UnknownValueError:
            logger.debug(f"[wake #{window}] silence")
        except sr.RequestError as e:
            logger.error(f"[wake #{window}] STT service error: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"[wake #{window}] error: {e}")
            time.sleep(2)


# ── Scheduler ──────────────────────────────────────────────────────────────────

def run_scheduler():
    schedule.every().day.at("08:00").do(morning_briefing)
    schedule.every().day.at("08:15").do(alert_arms_up)
    schedule.every().day.at("08:25").do(alert_ny_session_5min)
    schedule.every().day.at("09:25").do(alert_nyse_5min)
    schedule.every().day.at("11:00").do(alert_session_closed)
    schedule.every().day.at("16:00").do(eod_debrief)
    logger.info("Scheduler armed: 08:00 / 08:15 / 08:25 / 09:25 / 11:00 / 16:00")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── NOVA GUI ───────────────────────────────────────────────────────────────────

class NOVAApp:
    # Canvas dimensions
    W        = 720
    H        = 560
    FRAME_MS = 28   # ~35 fps

    # Orb
    ORB_CX   = 360
    ORB_CY   = 260
    ORB_R    = 54   # core radius

    # Waveform
    NUM_BARS = 36
    BAR_AREA_Y = 370   # top of waveform area
    BAR_H_MAX  = 52
    BAR_GAP    = 4
    LERP       = 0.20

    def __init__(self, root: tk.Tk):
        self.root   = root
        self.color  = C_CYAN
        self.mode   = "idle"
        self.phase  = 0.0
        self.heights        = [2.0] * self.NUM_BARS
        self.target_heights = [2.0] * self.NUM_BARS
        self.status_text    = "INITIALISING..."

        self._build_ui()
        self._animate()

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
        h = hex_color.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    @staticmethod
    def _rgb_to_hex(r: int, g: int, b: int) -> str:
        return f"#{r:02x}{g:02x}{b:02x}"

    def _dim(self, hex_color: str, factor: float) -> str:
        """Return hex_color darkened by factor (0=black, 1=original)."""
        r, g, b = self._hex_to_rgb(hex_color)
        return self._rgb_to_hex(
            int(r * factor), int(g * factor), int(b * factor)
        )

    # ── build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.title("NOVA")
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)

        self.canvas = tk.Canvas(
            self.root,
            width=self.W, height=self.H,
            bg=C_BG, highlightthickness=0,
        )
        self.canvas.pack()

        # ── HUD corner brackets ────────────────────────────────────────────────
        BL = 28   # bracket leg length
        BT = 2    # bracket thickness
        M  = 14   # margin from edge
        corners = [
            # top-left
            [(M, M+BL, M, M, M+BL, M)],
            # top-right
            [(self.W-M-BL, M, self.W-M, M, self.W-M, M+BL)],
            # bottom-left
            [(M, self.H-M-BL, M, self.H-M, M+BL, self.H-M)],
            # bottom-right
            [(self.W-M-BL, self.H-M, self.W-M, self.H-M, self.W-M, self.H-M-BL)],
        ]
        for poly in corners:
            self.canvas.create_line(*poly[0], fill=C_CYAN, width=BT)

        # ── Title — layered for glow effect ───────────────────────────────────
        tx, ty = self.W // 2, 42
        for offset, alpha in [(3, 0.12), (2, 0.22), (1, 0.45)]:
            col = self._dim(C_CYAN, alpha)
            for dx in (-offset, 0, offset):
                for dy in (-offset, 0, offset):
                    if dx == 0 and dy == 0:
                        continue
                    self.canvas.create_text(
                        tx + dx, ty + dy,
                        text="N.O.V.A.",
                        font=("Courier New", 30, "bold"),
                        fill=col, anchor="center",
                    )
        self.title_id = self.canvas.create_text(
            tx, ty,
            text="N.O.V.A.",
            font=("Courier New", 30, "bold"),
            fill=C_CYAN, anchor="center",
        )

        # ── Subtitle ──────────────────────────────────────────────────────────
        self.canvas.create_text(
            self.W // 2, 76,
            text="Neural Operations and Voice Assistant",
            font=("Courier New", 9),
            fill=C_SUBTITLE, anchor="center",
        )

        # ── Thin horizontal rule below subtitle ───────────────────────────────
        self.canvas.create_line(
            60, 96, self.W - 60, 96,
            fill=self._dim(C_CYAN, 0.12), width=1,
        )

        # ── Orb — concentric glow rings (outermost to innermost) ──────────────
        self.glow_ids = []
        glow_layers = [
            (self.ORB_R + 38, 0.04),
            (self.ORB_R + 26, 0.08),
            (self.ORB_R + 16, 0.14),
            (self.ORB_R + 8,  0.24),
            (self.ORB_R + 3,  0.45),
        ]
        for radius, alpha in glow_layers:
            gid = self.canvas.create_oval(
                self.ORB_CX - radius, self.ORB_CY - radius,
                self.ORB_CX + radius, self.ORB_CY + radius,
                fill=self._dim(C_CYAN, alpha), outline="",
            )
            self.glow_ids.append((gid, radius, alpha))

        # ── Orb core ──────────────────────────────────────────────────────────
        self.orb_id = self.canvas.create_oval(
            self.ORB_CX - self.ORB_R, self.ORB_CY - self.ORB_R,
            self.ORB_CX + self.ORB_R, self.ORB_CY + self.ORB_R,
            fill=self._dim(C_CYAN, 0.70), outline=C_CYAN, width=1,
        )

        # ── Waveform bars ─────────────────────────────────────────────────────
        total_w  = self.W - 120
        bar_slot = total_w / self.NUM_BARS
        bar_w    = max(1, bar_slot - self.BAR_GAP)
        x_start  = 60

        self.bar_ids = []
        for i in range(self.NUM_BARS):
            x0  = x_start + i * bar_slot + self.BAR_GAP / 2
            x1  = x0 + bar_w
            by  = self.BAR_AREA_Y + self.BAR_H_MAX // 2
            bid = self.canvas.create_rectangle(
                x0, by - 2, x1, by + 2,
                fill=self._dim(C_CYAN, 0.5), outline="",
            )
            self.bar_ids.append(bid)

        # ── Thin rule above status ─────────────────────────────────────────────
        self.canvas.create_line(
            60, self.H - 52, self.W - 60, self.H - 52,
            fill=self._dim(C_CYAN, 0.12), width=1,
        )

        # ── Status text ───────────────────────────────────────────────────────
        self.status_id = self.canvas.create_text(
            self.W // 2, self.H - 30,
            text=self.status_text,
            font=("Courier New", 8),
            fill=C_TEXT, anchor="center",
        )

    # ── animation loop ────────────────────────────────────────────────────────

    def _animate(self):
        # ── Drain GUI queue ────────────────────────────────────────────────────
        while not _gui_queue.empty():
            try:
                msg        = _gui_queue.get_nowait()
                self.color = msg.get("color", self.color)
                self.mode  = msg.get("mode",  self.mode)
                status     = msg.get("status", "")
                if status:
                    self.status_text = status.upper()
                    self.canvas.itemconfig(self.status_id, text=self.status_text)
            except queue.Empty:
                break

        self.phase += 0.07

        # ── Orb pulse ──────────────────────────────────────────────────────────
        if self.mode == "speaking":
            # Rapid energetic pulse
            pulse = 0.85 + 0.22 * abs(math.sin(self.phase * 3.2 + random.uniform(-0.1, 0.1)))
        elif self.mode in ("alert", "trade"):
            # Sharp strobe-like pulse
            pulse = 0.80 + 0.28 * abs(math.sin(self.phase * 4.5))
        elif self.mode == "listening":
            # Slow breath
            pulse = 0.88 + 0.14 * math.sin(self.phase * 0.9)
        else:
            # Very slow idle breath
            pulse = 0.92 + 0.08 * math.sin(self.phase * 0.4)

        r_core = self.ORB_R * pulse
        cx, cy = self.ORB_CX, self.ORB_CY
        self.canvas.coords(
            self.orb_id,
            cx - r_core, cy - r_core, cx + r_core, cy + r_core,
        )
        self.canvas.itemconfig(
            self.orb_id,
            fill=self._dim(self.color, 0.55),
            outline=self.color,
        )

        # ── Glow rings ─────────────────────────────────────────────────────────
        glow_pulse = pulse * (1.0 + 0.08 * math.sin(self.phase * 1.1))
        for gid, base_r, alpha in self.glow_ids:
            r = base_r * glow_pulse
            # Alert/trade: boost glow brightness
            a = alpha * (1.6 if self.mode in ("alert", "trade") else 1.0)
            a = min(a, 1.0)
            self.canvas.coords(gid, cx - r, cy - r, cx + r, cy + r)
            self.canvas.itemconfig(gid, fill=self._dim(self.color, a))

        # ── HUD brackets — flash on alert/trade ────────────────────────────────
        bracket_col = (
            self.color if self.mode in ("alert", "trade") and int(self.phase * 4) % 2 == 0
            else C_CYAN
        )
        for item in self.canvas.find_withtag("bracket"):
            self.canvas.itemconfig(item, fill=bracket_col)

        # ── Waveform bars ──────────────────────────────────────────────────────
        total_w  = self.W - 120
        bar_slot = total_w / self.NUM_BARS
        bar_w    = max(1, bar_slot - self.BAR_GAP)
        x_start  = 60
        by_mid   = self.BAR_AREA_Y + self.BAR_H_MAX // 2

        for i in range(self.NUM_BARS):
            if self.mode == "speaking":
                if random.random() < 0.40:
                    self.target_heights[i] = random.uniform(6, self.BAR_H_MAX)
            elif self.mode in ("alert", "trade"):
                amp = self.BAR_H_MAX * 0.85
                self.target_heights[i] = amp * abs(
                    math.sin(self.phase * 2.2 + i * math.pi / self.NUM_BARS)
                )
            elif self.mode == "listening":
                amp = self.BAR_H_MAX * 0.38
                self.target_heights[i] = amp * abs(
                    math.sin(self.phase * 0.8 + i * (math.pi * 2 / self.NUM_BARS))
                ) + 3
            else:  # idle
                self.target_heights[i] = (self.BAR_H_MAX * 0.08) * abs(
                    math.sin(self.phase * 0.3 + i * (math.pi / self.NUM_BARS))
                ) + 2

            self.heights[i] += (self.target_heights[i] - self.heights[i]) * self.LERP
            h   = max(2.0, self.heights[i])
            x0  = x_start + i * bar_slot + self.BAR_GAP / 2
            x1  = x0 + bar_w
            # Bars slightly dimmer than orb colour for depth
            bar_col = self._dim(self.color, 0.65)
            self.canvas.coords(self.bar_ids[i], x0, by_mid - h, x1, by_mid + h)
            self.canvas.itemconfig(self.bar_ids[i], fill=bar_col)

        # ── Title glow pulse ───────────────────────────────────────────────────
        title_col = self._dim(
            self.color,
            0.75 + 0.25 * (0.5 + 0.5 * math.sin(self.phase * 0.6)),
        )
        self.canvas.itemconfig(self.title_id, fill=title_col)

        self.root.after(self.FRAME_MS, self._animate)


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app  = NOVAApp(root)   # noqa: F841

    def _startup():
        time.sleep(0.8)
        speak(
            f"NOVA local interface online. Good morning Sir. "
            f"Say '{WAKE_PHRASE}' to activate me at any time."
        )

    for name, fn in [
        ("startup",       _startup),
        ("wake-word",     run_wake_word),
        ("scheduler",     run_scheduler),
        ("trade-monitor", run_trade_monitor),
    ]:
        threading.Thread(target=fn, daemon=True, name=name).start()

    root.mainloop()


if __name__ == "__main__":
    main()
