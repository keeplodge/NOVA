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

import pygame
import requests
import schedule
import speech_recognition as sr
from dotenv import load_dotenv

# ── Import all logic from nova_assistant ───────────────────────────────────────
# We import the module (not from), then patch speak() and listen_response()
# so every briefing/alert call in nova_assistant routes through the GUI version.
import nova_assistant

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = "7p1Ofvcwsv7UBPoFNcpI"
WAKE_PHRASE         = "alright nova lets cook"
NOVA_SERVER_URL     = os.environ.get(
    "NOVA_SERVER_URL", "https://nova-production-72f5.up.railway.app"
)
EST = ZoneInfo("America/New_York")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("nova_local.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ── Colours ────────────────────────────────────────────────────────────────────
C_BG       = "#0a0a0a"
C_CYAN     = "#00E5FF"   # listening
C_WHITE    = "#FFFFFF"   # speaking
C_GREEN    = "#00FF88"   # trade detected
C_RED      = "#FF4444"   # alert / warning
C_TEXT     = "#666666"
C_TITLE    = "#FFFFFF"
C_SUBTITLE = "#333333"

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
    """STT response capture, patched onto nova_assistant."""
    _push("listening", C_CYAN, "Listening for response...")
    try:
        recognizer = sr.Recognizer()
        mic        = sr.Microphone()
        with mic as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=20)
        return recognizer.recognize_google(audio).lower()
    except Exception:
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

def run_wake_word():
    recognizer = sr.Recognizer()
    recognizer.energy_threshold         = 3000
    recognizer.dynamic_energy_threshold = True
    mic = sr.Microphone()

    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=2)

    _push("listening", C_CYAN, "Listening for wake word...")
    logger.info(f"Wake word listener active — say: '{WAKE_PHRASE}'")

    while True:
        try:
            with mic as source:
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=8)
            text = recognizer.recognize_google(audio).lower()
            logger.info(f"Heard: {text}")
            if WAKE_PHRASE in text:
                logger.info("Wake word detected")
                speak("Sir. Ready. What do you need?")
        except sr.WaitTimeoutError:
            pass
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            logger.error(f"STT service error: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Wake word error: {e}")
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
    NUM_BARS  = 40
    BAR_GAP   = 3
    CANVAS_W  = 680
    CANVAS_H  = 180
    LERP      = 0.22
    FRAME_MS  = 33          # ~30 fps

    def __init__(self, root: tk.Tk):
        self.root    = root
        self.color   = C_CYAN
        self.mode    = "idle"
        self.phase   = 0.0
        self.heights        = [3.0] * self.NUM_BARS
        self.target_heights = [3.0] * self.NUM_BARS

        self._build_ui()
        self._animate()

    def _build_ui(self):
        self.root.title("NOVA")
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)

        # ── Title ──────────────────────────────────────────────────────────────
        tk.Label(
            self.root,
            text="N . O . V . A .",
            font=("Courier New", 34, "bold"),
            fg=C_TITLE, bg=C_BG,
        ).pack(pady=(32, 6))

        # ── Subtitle ───────────────────────────────────────────────────────────
        tk.Label(
            self.root,
            text="Network Operations and Voice Assistant",
            font=("Courier New", 10),
            fg=C_SUBTITLE, bg=C_BG,
        ).pack(pady=(0, 22))

        # ── Waveform canvas ────────────────────────────────────────────────────
        self.canvas = tk.Canvas(
            self.root,
            width=self.CANVAS_W, height=self.CANVAS_H,
            bg=C_BG, highlightthickness=0,
        )
        self.canvas.pack()

        bar_slot = self.CANVAS_W / self.NUM_BARS
        bar_w    = bar_slot - self.BAR_GAP
        cy       = self.CANVAS_H // 2

        self.bar_ids = []
        for i in range(self.NUM_BARS):
            x0  = i * bar_slot + self.BAR_GAP / 2
            x1  = x0 + bar_w
            bid = self.canvas.create_rectangle(
                x0, cy - 3, x1, cy + 3,
                fill=self.color, outline="",
            )
            self.bar_ids.append(bid)

        # ── Status label ───────────────────────────────────────────────────────
        self.status_var = tk.StringVar(value="INITIALISING...")
        tk.Label(
            self.root,
            textvariable=self.status_var,
            font=("Courier New", 9),
            fg=C_TEXT, bg=C_BG,
        ).pack(pady=(18, 28))

    def _animate(self):
        # Drain GUI queue
        while not _gui_queue.empty():
            try:
                msg        = _gui_queue.get_nowait()
                self.color = msg.get("color", self.color)
                self.mode  = msg.get("mode",  self.mode)
                status     = msg.get("status", "")
                if status:
                    self.status_var.set(status.upper())
            except queue.Empty:
                break

        self.phase += 0.10
        cy       = self.CANVAS_H // 2
        bar_slot = self.CANVAS_W / self.NUM_BARS
        bar_w    = bar_slot - self.BAR_GAP

        for i in range(self.NUM_BARS):
            # Compute target height by mode
            if self.mode == "speaking":
                if random.random() < 0.35:
                    self.target_heights[i] = random.uniform(12, cy - 8)
            elif self.mode in ("alert", "trade"):
                amp = cy - 12
                self.target_heights[i] = amp * abs(
                    math.sin(self.phase + i * math.pi / self.NUM_BARS)
                )
            elif self.mode == "listening":
                amp = 28
                self.target_heights[i] = amp * abs(
                    math.sin(self.phase * 0.7 + i * (math.pi * 2 / self.NUM_BARS))
                )
            else:  # idle
                self.target_heights[i] = 5 * abs(
                    math.sin(self.phase * 0.25 + i * (math.pi / self.NUM_BARS))
                ) + 2

            # Lerp toward target
            self.heights[i] += (self.target_heights[i] - self.heights[i]) * self.LERP
            h  = max(2.0, self.heights[i])
            x0 = i * bar_slot + self.BAR_GAP / 2
            x1 = x0 + bar_w
            self.canvas.coords(self.bar_ids[i], x0, cy - h, x1, cy + h)
            self.canvas.itemconfig(self.bar_ids[i], fill=self.color)

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
