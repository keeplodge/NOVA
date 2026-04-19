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

import asyncio
import re
import subprocess
import sys
import webbrowser

import anthropic
import edge_tts
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
from trading_agent import TradingAgent

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
EDGE_TTS_VOICE    = "en-GB-RyanNeural"   # British male — Microsoft neural (free)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
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
- Businesses: NOVA (trading product on Whop), Hunnid Ticks (trading Discord), KeepLodge (SaaS STR platform)
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

# ── Colours — mirrors the Neural Brain design system tokens ───────────────────
C_BG          = "#0A1929"   # Blueprint Navy — primary surface
C_BG_ALT      = "#0F2238"   # Navy 2 — card surfaces
C_BORDER      = "#1A2F47"   # Navy border — panel edges
C_GRID        = "#0f253d"   # Faint blueprint grid line
C_CYAN        = "#00d4ff"   # listening / default idle
C_WHITE       = "#FFFFFF"   # speaking
C_GREEN       = "#00ff88"   # trade detected / session active / in profit
C_RED         = "#ff3355"   # alert / loss warning
C_ORANGE      = "#FF6B00"   # Signal Orange — CTAs only (used for node accents)
C_STEEL       = "#A0AEC0"   # secondary text on dark
C_STEEL_DARK  = "#4A5568"   # tertiary text
C_TEXT        = "#4a6a7a"
C_TITLE       = "#FFFFFF"   # title is pure white with orange accent
C_SUBTITLE    = "#A0AEC0"
C_BRACKET     = "#1A2F47"   # HUD corner brackets (subtle)

# Adaptive tint palette — used by the market-state poller when assistant is idle
C_AMBER       = "#ff9d00"   # high VIX / elevated volatility
C_ORANGE_WARN = "#ff7a2e"   # approaching daily loss cap
C_TEAL_CALM   = "#00a8b8"   # low VIX / calm market
C_PURPLE_LIVE = "#b072ff"   # session is actively open

# ── Thread-safe GUI state queue ────────────────────────────────────────────────
_gui_queue  = queue.Queue()
_speak_lock = threading.Lock()


def _push(mode: str, color: str, status: str = ""):
    """Push a waveform / status update from any thread to the GUI."""
    _gui_queue.put({"mode": mode, "color": color, "status": status})


def _push_color_only(color: str, status: str = ""):
    """Update color and optionally status without changing mode. Used by the
    market-state poller so an active 'listening' or 'speaking' mode keeps its
    animation while the idle tint adapts to market conditions underneath."""
    payload = {"color": color}
    if status:
        payload["status"] = status
    _gui_queue.put(payload)


# ── Market-state poller — makes the waveform biometric ─────────────────────────
#
# Every MARKET_POLL_INTERVAL seconds this thread samples:
#   - VIX (yfinance ^VIX latest close)
#   - NOVA /status (daily_loss, loss_remaining, active_session)
# and pushes a color tint into the GUI queue based on priority rules:
#
#   1. daily_loss >= 300      → RED   (loss warning)
#   2. loss_remaining <= 200  → ORANGE_WARN (near cap)
#   3. active_session != None → PURPLE (session live)
#   4. VIX >= 25              → AMBER (elevated volatility)
#   5. VIX <= 13              → TEAL  (calm)
#   6. otherwise              → CYAN  (default idle)

MARKET_POLL_INTERVAL = 60   # seconds
_MARKET_STATUS_URL   = os.environ.get("NOVA_SERVER_URL", "https://nova-production-72f5.up.railway.app") + "/status"


def _fetch_vix_latest() -> float | None:
    try:
        import yfinance as yf
        t = yf.Ticker("^VIX")
        hist = t.history(period="1d", interval="5m")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def _fetch_nova_status() -> dict | None:
    try:
        r = requests.get(_MARKET_STATUS_URL, timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _pick_tint_from_state(vix: float | None, status: dict | None) -> tuple[str, str]:
    """Returns (color, status_label) based on market + account state."""
    if status:
        loss      = status.get("daily_loss", 0.0) or 0.0
        remaining = status.get("loss_remaining", 500.0) or 500.0
        session   = status.get("active_session") or ""
        if loss >= 300:
            return C_RED, f"LOSS ${loss:.0f} / ${loss + remaining:.0f}"
        if remaining <= 200:
            return C_ORANGE_WARN, f"NEAR CAP — ${remaining:.0f} LEFT"
        if session and session.lower() != "none":
            return C_PURPLE_LIVE, f"{session.upper()} LIVE"
    if vix is not None:
        if vix >= 25:
            return C_AMBER, f"VIX {vix:.1f} — ELEVATED"
        if vix <= 13:
            return C_TEAL_CALM, f"VIX {vix:.1f} — CALM"
    return C_CYAN, ""


def _push_state(state: dict):
    """Merge a live_state patch into the GUI queue — goes into stat panels."""
    _gui_queue.put({"state": state})


def _brain_stats() -> tuple[str, str]:
    """Return (brain_status_label, memory_count_label). Never raises."""
    try:
        import sys as _s
        _bp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neural-brain", "backend")
        if _bp not in _s.path:
            _s.path.insert(0, _bp)
        from brain_bridge import sync_online, sync_recent
        if not sync_online():
            return "OFFLINE", "--"
        recent = sync_recent(limit=200)
        return "ONLINE", str(len(recent))
    except Exception:
        return "OFFLINE", "--"


def market_state_poller_loop():
    """
    Background thread — polls market + account state every 60s. Drives both
    the adaptive-tint color AND the side stat panels via live_state patches.
    """
    logger.info("Market state poller armed — tinting waveform + live stats.")
    while True:
        try:
            vix    = _fetch_vix_latest()
            status = _fetch_nova_status()
            color, label = _pick_tint_from_state(vix, status)

            # Tint the waveform
            _push_color_only(color, label)

            # Populate the side stat panels
            brain_status, brain_mems = _brain_stats()
            patch = {
                "session":        (status or {}).get("active_session", "None") or "None",
                "vix":            f"{vix:.1f}" if vix is not None else "--",
                "daily_loss":     f"${(status or {}).get('daily_loss', 0):.0f}",
                "remaining":      f"${(status or {}).get('loss_remaining', 500):.0f}",
                "trades_today":   str((status or {}).get("trades_today", 0)),
                "brain_status":   brain_status,
                "brain_memories": brain_mems,
            }

            # Try to fetch NQ price separately (falls back to --)
            try:
                import yfinance as yf
                nq_t = yf.Ticker("NQ=F")
                nq_hist = nq_t.history(period="1d", interval="5m")
                if nq_hist is not None and not nq_hist.empty:
                    patch["nq_price"] = f"{float(nq_hist['Close'].iloc[-1]):.1f}"
            except Exception:
                pass

            _push_state(patch)
        except Exception as e:
            logger.debug(f"market state poll failed: {e}")
        time.sleep(MARKET_POLL_INTERVAL)


BRAIN_URL = "http://localhost:7474"

def _brain_push(event_type: str, text: str = "", **extra):
    """Fire-and-forget: post a live event to the NOVA Brain dashboard."""
    def _post():
        try:
            requests.post(
                f"{BRAIN_URL}/api/event",
                json={"event_type": event_type, "text": text[:300], **extra},
                timeout=0.8,
            )
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()


def _brain_push_sync(event_type: str, text: str = "", **extra):
    """Synchronous brain push — used inside streaming where timing matters."""
    try:
        requests.post(
            f"{BRAIN_URL}/api/event",
            json={"event_type": event_type, "text": text, **extra},
            timeout=0.5,
        )
    except Exception:
        pass


def _start_brain_server():
    """Launch nova_brain.py then open the dashboard as a native desktop window."""
    here = os.path.dirname(os.path.abspath(__file__))
    brain_path = os.path.join(here, "nova_brain.py")
    webview_path = os.path.join(here, "nova_webview.py")
    if not os.path.exists(brain_path):
        logger.warning("nova_brain.py not found — brain dashboard not started.")
        return
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    subprocess.Popen(
        [sys.executable, brain_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    logger.info("NOVA Brain dashboard launching — http://localhost:7474")

    def _open_desktop_window():
        time.sleep(2)
        if os.path.exists(webview_path):
            subprocess.Popen(
                [sys.executable, webview_path, "--url", BRAIN_URL],
                creationflags=flags,
            )
        else:
            # Fallback to browser if webview script missing
            webbrowser.open(BRAIN_URL)

    threading.Thread(target=_open_desktop_window, daemon=True).start()


# ── GUI-aware speak() ──────────────────────────────────────────────────────────

def speak(text: str, _to_brain: bool = True):
    """
    edge-tts TTS with waveform state updates.
    Microsoft neural voice — free, no API key required.
    Patched onto nova_assistant so every briefing/alert call routes here.
    Pass _to_brain=False when called from _stream_ask_nova to avoid double-push.
    """
    logger.info(f"[NOVA]: {text}")
    _push("speaking", C_WHITE, (text[:72] + "...") if len(text) > 72 else text)
    if _to_brain:
        _brain_push("voice", text)

    with _speak_lock:
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()

        tmp_path = None
        try:
            tmp_path = tempfile.mktemp(suffix=".mp3")

            async def _synthesise():
                communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
                await communicate.save(tmp_path)

            asyncio.run(_synthesise())

            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
            pygame.mixer.music.unload()

        except Exception as e:
            logger.error(f"TTS error: {e}")
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


## run_trade_monitor replaced by TradingAgent (see main())


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


_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

def _split_sentences(buf: str) -> tuple[list[str], str]:
    """Split complete sentences out of a text buffer, return (sentences, remainder)."""
    parts = _SENTENCE_END.split(buf)
    if len(parts) <= 1:
        return [], buf
    complete = [s.strip() for s in parts[:-1] if s.strip()]
    return complete, parts[-1]


def _stream_ask_nova(user_message: str):
    """
    Stream Claude's response token-by-token to the brain dashboard while
    speaking sentence-by-sentence via TTS. No second API key — uses the same
    ANTHROPIC_API_KEY already in .env.

    Flow:
      1. brain_activate  → neural canvas lights up, holo ring activates
      2. stream_start    → brain shows streaming bubble
      3. stream_token ×N → tokens appear live in brain dashboard
      4. stream_end      → brain finalises, TTS finishes last sentence
    """
    if not ANTHROPIC_API_KEY:
        speak("Anthropic API key not configured, Sir.")
        return

    # ── Signal brain to activate ───────────────────────────────────────────────
    _brain_push_sync("brain_activate", query=user_message, knowledge_accessed=[])
    _brain_push_sync("stream_start")

    client_local = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    context = _build_live_context()
    full_message = f"Live data context:\n{context}\n\nSir's request: {user_message}"

    full_response = ""
    sentence_buf = ""
    tts_q: queue.Queue = queue.Queue()

    # ── TTS worker — speaks sentences in order without blocking the stream ─────
    def _tts_worker():
        while True:
            sentence = tts_q.get()
            if sentence is None:
                break
            speak(sentence, _to_brain=False)
            tts_q.task_done()

    tts_thread = threading.Thread(target=_tts_worker, daemon=True)
    tts_thread.start()

    try:
        with client_local.messages.stream(
            model="claude-opus-4-6",
            max_tokens=350,
            system=NOVA_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": full_message}],
        ) as stream:
            for token in stream.text_stream:
                full_response += token
                sentence_buf  += token

                # Push token to brain — dashboard sees it instantly
                _brain_push_sync("stream_token", token=token)

                # Queue complete sentences for TTS as they form
                sentences, sentence_buf = _split_sentences(sentence_buf)
                for s in sentences:
                    tts_q.put(s)

    except Exception as e:
        logger.error(f"Stream error: {e}")
        full_response = "I ran into an issue with my reasoning engine, Sir."
        tts_q.put(full_response)

    # Speak any remaining fragment
    remainder = sentence_buf.strip()
    if remainder:
        tts_q.put(remainder)

    # ── Signal brain stream is complete ───────────────────────────────────────
    _brain_push_sync("stream_end", full_response=full_response)

    # Wait for all TTS to finish before returning
    tts_q.put(None)
    tts_thread.join(timeout=120)

    _push("listening", C_CYAN, "Listening...")


def _handle_command(cmd: str):
    """
    Dispatch a recognised voice command to the appropriate response.
    Runs in the wake-word thread — speak() is thread-safe.
    """
    cmd = cmd.strip().lower()
    logger.info(f"[cmd] \"{cmd}\"")
    _brain_push("command", f"SIR: {cmd}")

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

    # ── fallback — stream through Claude, live to brain dashboard + TTS ─────────
    _push("speaking", C_WHITE, "Thinking...")
    _stream_ask_nova(cmd)


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
    # Canvas dimensions — wider dashboard layout matching the Brain aesthetic
    W        = 1000
    H        = 680
    FRAME_MS = 28   # ~35 fps

    # Orb — centered in main canvas area
    ORB_CX   = 500
    ORB_CY   = 320
    ORB_R    = 70

    # Waveform
    NUM_BARS = 48
    BAR_AREA_Y = 440
    BAR_H_MAX  = 56
    BAR_GAP    = 4
    LERP       = 0.20

    # Grid pattern (Blueprint texture)
    GRID_STEP = 48

    # Side stat panels
    PANEL_W      = 220
    PANEL_MARGIN = 28
    PANEL_TOP    = 120
    PANEL_H      = 310

    # Neural-link strip (bottom)
    STRIP_H = 44

    def __init__(self, root: tk.Tk):
        self.root   = root
        self.color  = C_CYAN
        self.mode   = "idle"
        self.phase  = 0.0
        self.heights        = [2.0] * self.NUM_BARS
        self.target_heights = [2.0] * self.NUM_BARS
        self.status_text    = "INITIALISING..."

        # Live data cached by the market poller — shown in the side panels
        self.live_state = {
            "session":       "None",
            "nq_price":      "--",
            "vix":           "--",
            "daily_loss":    "$0",
            "trades_today":  "0",
            "remaining":     "$500",
            "brain_status":  "CHECKING",
            "brain_memories": "--",
        }

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

        # ── Blueprint grid — faint hairlines every GRID_STEP px ───────────────
        for x in range(0, self.W, self.GRID_STEP):
            self.canvas.create_line(x, 0, x, self.H, fill=C_GRID, width=1)
        for y in range(0, self.H, self.GRID_STEP):
            self.canvas.create_line(0, y, self.W, y, fill=C_GRID, width=1)

        # ── Top bar: logo mark + wordmark + subtitle ──────────────────────────
        # Logo mark (orange square with "N" inside) — mirrors the Brain's design
        mx, my = 36, 44
        self.canvas.create_rectangle(
            mx - 20, my - 20, mx + 20, my + 20,
            fill=C_ORANGE, outline="",
        )
        self.canvas.create_text(
            mx, my + 1,
            text="N",
            font=("Arial Black", 20),
            fill=C_WHITE, anchor="center",
        )
        # Wordmark
        self.canvas.create_text(
            70, 34,
            text="NOVA",
            font=("Arial Black", 18),
            fill=C_WHITE, anchor="w",
        )
        self.canvas.create_text(
            70, 56,
            text="ASSISTANT",
            font=("Consolas", 9, "bold"),
            fill=C_ORANGE, anchor="w",
        )
        # Subtitle (right side — status overline)
        self.canvas.create_text(
            self.W - 28, 44,
            text="NEURAL OPERATIONS · VOICE ASSISTANT · V2",
            font=("Consolas", 9, "bold"),
            fill=C_STEEL_DARK, anchor="e",
        )

        # ── Horizontal divider under header ───────────────────────────────────
        self.canvas.create_line(
            0, 84, self.W, 84,
            fill=C_BORDER, width=1,
        )

        # ── Title — massive centered, layered glow ────────────────────────────
        tx, ty = self.W // 2, 112
        for offset, alpha in [(2, 0.22), (1, 0.45)]:
            col = self._dim(C_WHITE, alpha)
            for dx in (-offset, 0, offset):
                for dy in (-offset, 0, offset):
                    if dx == 0 and dy == 0:
                        continue
                    self.canvas.create_text(
                        tx + dx, ty + dy,
                        text="N · O · V · A",
                        font=("Arial Black", 26),
                        fill=col, anchor="center",
                    )
        self.title_id = self.canvas.create_text(
            tx, ty,
            text="N · O · V · A",
            font=("Arial Black", 26),
            fill=C_TITLE, anchor="center",
        )

        # ── LEFT stat panel — market state ────────────────────────────────────
        self._build_stat_panel(
            x=self.PANEL_MARGIN, y=self.PANEL_TOP,
            w=self.PANEL_W, h=self.PANEL_H,
            heading="MARKET STATE",
            heading_color=C_ORANGE,
            rows=[
                ("SESSION",  "session",      C_WHITE),
                ("NQ",       "nq_price",     C_WHITE),
                ("VIX",      "vix",          C_WHITE),
                ("STATUS",   "brain_status", C_CYAN),
            ],
        )

        # ── RIGHT stat panel — account + activity ─────────────────────────────
        self._build_stat_panel(
            x=self.W - self.PANEL_MARGIN - self.PANEL_W, y=self.PANEL_TOP,
            w=self.PANEL_W, h=self.PANEL_H,
            heading="TODAY",
            heading_color=C_ORANGE,
            rows=[
                ("DAILY LOSS",    "daily_loss",    C_RED),
                ("REMAINING",     "remaining",     C_GREEN),
                ("TRADES TODAY",  "trades_today",  C_WHITE),
                ("MEMORIES",      "brain_memories", C_CYAN),
            ],
        )

        # ── Wireframe orb — nodes + edges (Fibonacci-distributed) ─────────────
        self._build_wireframe_orb()

        # ── Waveform bars (under the orb) ─────────────────────────────────────
        wave_left  = self.PANEL_MARGIN + self.PANEL_W + 24
        wave_right = self.W - self.PANEL_MARGIN - self.PANEL_W - 24
        total_w    = wave_right - wave_left
        bar_slot   = total_w / self.NUM_BARS
        bar_w      = max(1, bar_slot - self.BAR_GAP)

        self.bar_ids = []
        for i in range(self.NUM_BARS):
            x0  = wave_left + i * bar_slot + self.BAR_GAP / 2
            x1  = x0 + bar_w
            by  = self.BAR_AREA_Y + self.BAR_H_MAX // 2
            bid = self.canvas.create_rectangle(
                x0, by - 2, x1, by + 2,
                fill=self._dim(C_CYAN, 0.5), outline="",
            )
            self.bar_ids.append(bid)
        self._bar_bounds = (wave_left, wave_right)

        # ── Neural-link strip (bottom) ────────────────────────────────────────
        strip_y = self.H - self.STRIP_H
        self.canvas.create_rectangle(
            0, strip_y, self.W, self.H,
            fill=C_BG_ALT, outline="",
        )
        self.canvas.create_line(
            0, strip_y, self.W, strip_y,
            fill=C_BORDER, width=1,
        )
        # Left: brain link label with pulse dot
        self._link_dot = self.canvas.create_oval(
            self.PANEL_MARGIN, strip_y + 18,
            self.PANEL_MARGIN + 10, strip_y + 28,
            fill=C_GREEN, outline="",
        )
        self.canvas.create_text(
            self.PANEL_MARGIN + 20, strip_y + 23,
            text="LINKED TO NEURAL BRAIN",
            font=("Consolas", 10, "bold"),
            fill=C_STEEL, anchor="w",
        )
        # Center: current status (animated)
        self.status_id = self.canvas.create_text(
            self.W // 2, strip_y + 23,
            text=self.status_text,
            font=("Consolas", 10, "bold"),
            fill=C_CYAN, anchor="center",
        )
        # Right: memory count + uptime
        self._uptime_id = self.canvas.create_text(
            self.W - self.PANEL_MARGIN, strip_y + 23,
            text="NOVA · V2 · ASSISTANT",
            font=("Consolas", 9, "bold"),
            fill=C_STEEL_DARK, anchor="e",
        )

    def _build_stat_panel(self, x: int, y: int, w: int, h: int,
                          heading: str, heading_color: str,
                          rows: list[tuple[str, str, str]]) -> None:
        """
        Stats panel — thin-bordered navy card with an orange overline heading
        and 4 rows of LABEL / value pairs. Matches the Brain's glass-panel
        aesthetic within Tkinter's capability envelope.
        """
        # Panel background
        self.canvas.create_rectangle(
            x, y, x + w, y + h,
            fill=C_BG_ALT, outline=C_BORDER, width=1,
        )
        # Heading (overline style)
        self.canvas.create_text(
            x + 16, y + 18,
            text=heading,
            font=("Consolas", 10, "bold"),
            fill=heading_color, anchor="w",
        )
        # Thin rule under heading
        self.canvas.create_line(
            x + 16, y + 36, x + w - 16, y + 36,
            fill=C_BORDER, width=1,
        )

        # Rows
        if not hasattr(self, "_stat_value_ids"):
            self._stat_value_ids = {}

        row_y  = y + 58
        row_dy = (h - 72) // max(len(rows), 1)
        for label, key, value_color in rows:
            self.canvas.create_text(
                x + 16, row_y,
                text=label,
                font=("Consolas", 9, "bold"),
                fill=C_STEEL_DARK, anchor="w",
            )
            val_id = self.canvas.create_text(
                x + w - 16, row_y,
                text=self.live_state.get(key, "--"),
                font=("Consolas", 12, "bold"),
                fill=value_color, anchor="e",
            )
            self._stat_value_ids[key] = val_id
            # Divider under each row except the last
            if (label, key, value_color) != rows[-1]:
                self.canvas.create_line(
                    x + 16, row_y + row_dy // 2,
                    x + w - 16, row_y + row_dy // 2,
                    fill=self._dim(C_BORDER, 0.5), width=1,
                )
            row_y += row_dy

    def _build_wireframe_orb(self):
        """
        Wireframe orb — 14 Fibonacci-distributed nodes on a 2D projection of
        a sphere, connected by nearest-neighbor edges. Visually echoes the
        Neural Brain's WebGL wireframe orb within Tkinter's 2D constraints.
        """
        import math as _math

        cx, cy, R = self.ORB_CX, self.ORB_CY, self.ORB_R
        N = 14
        golden = _math.pi * (3 - _math.sqrt(5))
        nodes_2d = []  # list of (x, y)
        for i in range(N):
            y_norm = 1 - (i / (N - 1)) * 2                # -1..1
            r_ring = _math.sqrt(1 - y_norm * y_norm)
            theta  = golden * i
            x_proj = _math.cos(theta) * r_ring * R
            y_proj = y_norm * R
            nodes_2d.append((cx + x_proj, cy + y_proj))

        # Concentric glow rings (behind the wireframe)
        self.glow_ids = []
        glow_layers = [
            (R + 46, 0.03),
            (R + 32, 0.07),
            (R + 20, 0.12),
            (R + 10, 0.20),
        ]
        for radius, alpha in glow_layers:
            gid = self.canvas.create_oval(
                cx - radius, cy - radius, cx + radius, cy + radius,
                fill=self._dim(C_CYAN, alpha), outline="",
            )
            self.glow_ids.append((gid, radius, alpha))

        # Edges — each node connects to its 3 nearest neighbors, deduped
        self.orb_edge_ids = []
        edge_set = set()
        for i, (xi, yi) in enumerate(nodes_2d):
            d = sorted(
                ((j, (xi - nx)**2 + (yi - ny)**2) for j, (nx, ny) in enumerate(nodes_2d) if j != i),
                key=lambda t: t[1],
            )
            for j, _ in d[:3]:
                key = (min(i, j), max(i, j))
                if key in edge_set:
                    continue
                edge_set.add(key)
                xj, yj = nodes_2d[j]
                eid = self.canvas.create_line(
                    xi, yi, xj, yj,
                    fill=self._dim(C_CYAN, 0.45), width=1,
                )
                self.orb_edge_ids.append(eid)

        # Node dots
        self.orb_node_ids = []
        for (nx, ny) in nodes_2d:
            nid = self.canvas.create_oval(
                nx - 3, ny - 3, nx + 3, ny + 3,
                fill=C_CYAN, outline="",
            )
            self.orb_node_ids.append(nid)

        # Inner bright core
        self.orb_id = self.canvas.create_oval(
            cx - 10, cy - 10, cx + 10, cy + 10,
            fill=C_WHITE, outline="",
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
                state_patch = msg.get("state") or {}
                if state_patch:
                    self.live_state.update(state_patch)
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

        cx, cy = self.ORB_CX, self.ORB_CY

        # ── Inner bright core — small pulsing dot (not full orb radius) ───────
        core_r = 7 + 4 * pulse
        self.canvas.coords(
            self.orb_id,
            cx - core_r, cy - core_r, cx + core_r, cy + core_r,
        )
        self.canvas.itemconfig(self.orb_id, fill=self.color)

        # ── Glow rings ─────────────────────────────────────────────────────────
        glow_pulse = pulse * (1.0 + 0.08 * math.sin(self.phase * 1.1))
        for gid, base_r, alpha in self.glow_ids:
            r = base_r * glow_pulse
            a = alpha * (1.6 if self.mode in ("alert", "trade") else 1.0)
            a = min(a, 1.0)
            self.canvas.coords(gid, cx - r, cy - r, cx + r, cy + r)
            self.canvas.itemconfig(gid, fill=self._dim(self.color, a))

        # ── Wireframe edges — pulse opacity with activity ─────────────────────
        edge_alpha = 0.3 + 0.2 * (0.5 + 0.5 * math.sin(self.phase * 0.9))
        if self.mode in ("alert", "trade"):
            edge_alpha = 0.7
        edge_col = self._dim(self.color, edge_alpha)
        if hasattr(self, "orb_edge_ids"):
            for eid in self.orb_edge_ids:
                self.canvas.itemconfig(eid, fill=edge_col)

        # ── Wireframe nodes — slight size pulse, bright color ─────────────────
        node_col = C_WHITE if self.mode in ("speaking", "alert", "trade") else self.color
        if hasattr(self, "orb_node_ids"):
            for nid in self.orb_node_ids:
                self.canvas.itemconfig(nid, fill=node_col)

        # ── Stat panels — refresh values from live_state every frame ──────────
        if hasattr(self, "_stat_value_ids"):
            for key, vid in self._stat_value_ids.items():
                val = str(self.live_state.get(key, "--"))
                try:
                    self.canvas.itemconfig(vid, text=val)
                except Exception:
                    pass

        # ── Neural-link dot — pulse green when brain online, red otherwise ────
        if hasattr(self, "_link_dot"):
            brain_ok = self.live_state.get("brain_status", "") == "ONLINE"
            link_col = C_GREEN if brain_ok else C_RED
            dot_pulse = 0.75 + 0.25 * abs(math.sin(self.phase * 1.6))
            self.canvas.itemconfig(self._link_dot, fill=self._dim(link_col, dot_pulse))

        # ── Status text color — match current state color ─────────────────────
        self.canvas.itemconfig(self.status_id, fill=self.color)

        # ── Waveform bars — bounded by the space between the stat panels ──────
        wave_left, wave_right = getattr(self, "_bar_bounds", (60, self.W - 60))
        total_w  = wave_right - wave_left
        bar_slot = total_w / self.NUM_BARS
        bar_w    = max(1, bar_slot - self.BAR_GAP)
        x_start  = wave_left
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
    _start_brain_server()

    root = tk.Tk()
    app  = NOVAApp(root)   # noqa: F841

    def _startup():
        time.sleep(0.8)
        speak(
            f"NOVA local interface online. Good morning Sir. "
            f"Say '{WAKE_PHRASE}' to activate me at any time."
        )
        _brain_push("system", "NOVA local interface online. Voice and trading agents armed.")

    # ── Trading agent — trade detection, session watch, loss alerts ──────────
    agent = TradingAgent(speak_fn=speak, push_gui_fn=_push, server_url=NOVA_SERVER_URL)
    agent.start()

    # ── Drift monitor — background win-rate / streak / drawdown watcher ──────
    try:
        from nova_drift_monitor import DriftMonitor
        DriftMonitor(speaker=speak).start()
    except Exception as _e:
        logger.warning(f"Drift monitor not started: {_e}")

    for name, fn in [
        ("startup",       _startup),
        ("wake-word",     run_wake_word),
        ("scheduler",     run_scheduler),
        ("market-state",  market_state_poller_loop),
    ]:
        threading.Thread(target=fn, daemon=True, name=name).start()

    root.mainloop()


if __name__ == "__main__":
    main()
