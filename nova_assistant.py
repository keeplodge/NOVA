#!/usr/bin/env python3
"""
NOVA Assistant — Personal AI voice assistant for Sir
Wake word : "alright nova lets cook"
Scheduled : 8:00am morning briefing + timed session alerts (EST)
Voice     : ElevenLabs Daniel British  (ID: 766NdLzxBMJanRvWXtkt)
"""

import logging
import os
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncio

import edge_tts
import numpy as np
import pygame
import requests
import schedule
import sounddevice as sd
import speech_recognition as sr
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

# ── KeepLodge Agent integrations ──────────────────────────────────────────────
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "keeplodge"))
try:
    from waitlist_agent import poll_and_process as _waitlist_poll
    from waitlist_agent import morning_briefing_report as _waitlist_report
    _WAITLIST_ENABLED = True
except ImportError as _e:
    _WAITLIST_ENABLED = False

try:
    from competitive_agent import morning_briefing_summary as _competitive_summary
    _COMPETITIVE_ENABLED = True
except ImportError:
    _COMPETITIVE_ENABLED = False

# ── Config ─────────────────────────────────────────────────────────────────────
EDGE_TTS_VOICE  = "en-GB-RyanNeural"   # British male — Microsoft neural (free)
FINNHUB_API_KEY = os.environ.get("FINNHUB_KEY", "")
NEWSAPI_KEY         = os.environ.get("NEWSAPI_KEY", "")
NOVA_SERVER_URL     = os.environ.get(
    "NOVA_SERVER_URL", "https://nova-production-72f5.up.railway.app"
)
WAKE_PHRASE = "nova"
EST         = ZoneInfo("America/New_York")

# ── Neural Brain Bridge ───────────────────────────────────────────────────────
import sys as _sys2
_BRAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neural-brain", "backend")
if _BRAIN_PATH not in _sys2.path:
    _sys2.path.insert(0, _BRAIN_PATH)
try:
    from brain_bridge import (
        sync_store as _brain_store,
        sync_search as _brain_search,
        classify as _brain_classify,
        remember as _brain_remember,
        remember_briefing as _brain_remember_briefing,
        remember_debrief as _brain_remember_debrief,
        remember_trade as _brain_remember_trade,
        context_block as _brain_context_block,
        sync_online as _brain_online,
    )
    _BRAIN_ENABLED = True
except ImportError:
    _BRAIN_ENABLED = False

# ── Claude-powered voice command intelligence ────────────────────────────────
try:
    from nova_command_ai import classify_and_respond as _nova_classify, handle_remember as _nova_cmd_remember, handle_recall as _nova_cmd_recall
    _COMMAND_AI_ENABLED = True
except ImportError:
    _COMMAND_AI_ENABLED = False

NQ_TECH_NAMES = {
    "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META",
    "NVDA", "TSLA", "NFLX", "AMD", "INTC", "QCOM",
}

# ── Logging ────────────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nova_assistant.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_LOG_PATH),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ── Text-to-Speech ─────────────────────────────────────────────────────────────

# Global lock — ensures only one speak() plays at a time across all threads
_speak_lock = threading.Lock()

# Initialise mixer once at startup rather than per-call
pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=512)
pygame.mixer.init()


def speak(text: str):
    """
    Convert text to speech via edge-tts (Microsoft neural, free, no API key).
    Thread-safe: acquires _speak_lock so concurrent calls queue instead of overlap.
    Also mirrors the spoken line into the browser dashboard chat log + pulses
    the orb into 'speaking' mode for the duration of playback.
    """
    logger.info(f"[NOVA]: {text}")

    # Mirror to the browser dashboard (fire-and-forget; no-op if UI offline)
    try:
        from nova_ui_client import push_log, push_mode
        push_log("nova", text)
        push_mode("speaking")
    except Exception:
        pass

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
            # Return orb to idle after the line finishes
            try:
                from nova_ui_client import push_mode
                push_mode("idle")
            except Exception:
                pass


# ── Voice Input ────────────────────────────────────────────────────────────────

SAMPLE_RATE = 16000   # Hz — matches Google STT expectation


def _capture_audio(duration: float) -> sr.AudioData:
    """
    Record `duration` seconds from the default mic using sounddevice.
    Returns an sr.AudioData object compatible with recognizer.recognize_google().
    No pyaudio required.
    """
    samples = sd.rec(
        int(duration * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
    )
    sd.wait()
    return sr.AudioData(samples.tobytes(), SAMPLE_RATE, 2)  # 2 bytes = int16


def listen_response(timeout: int = 15) -> str:
    """
    Record up to `timeout` seconds and transcribe via Google STT.
    Uses sounddevice — no pyaudio required.
    Returns empty string on silence or error.
    """
    try:
        recognizer = sr.Recognizer()
        audio = _capture_audio(float(timeout))
        text  = recognizer.recognize_google(audio).lower()
        logger.info(f"Response heard: {text}")
        return text
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        logger.error(f"STT request error: {e}")
        return ""
    except Exception as e:
        logger.error(f"listen_response error: {e}")
        return ""
        return ""


# ── Data Fetchers ──────────────────────────────────────────────────────────────

def get_weather() -> dict:
    """Fetch Toronto weather from wttr.in."""
    try:
        r = requests.get("https://wttr.in/Toronto?format=j1", timeout=10)
        r.raise_for_status()
        data    = r.json()
        current = data["current_condition"][0]
        today   = data["weather"][0]
        return {
            "desc":      current["weatherDesc"][0]["value"],
            "temp_c":    current["temp_C"],
            "feels_c":   current["FeelsLikeC"],
            "humidity":  current["humidity"],
            "max_c":     today["maxtempC"],
            "min_c":     today["mintempC"],
            "precip_mm": today["hourly"][0].get("precipMM", "0"),
        }
    except Exception as e:
        logger.error(f"Weather fetch error: {e}")
        return {}


def get_nq_full_data() -> dict:
    """NQ futures: current price, overnight range, key levels, change."""
    result = {
        "price": None, "change": None, "change_pct": None,
        "overnight_high": None, "overnight_low": None,
        "prev_high": None, "prev_low": None, "prev_close": None,
    }
    try:
        ticker     = yf.Ticker("NQ=F")
        hist_daily = ticker.history(period="5d", interval="1d")
        hist_1h    = ticker.history(period="2d", interval="1h")

        if len(hist_daily) >= 2:
            prev              = hist_daily.iloc[-2]
            result["prev_high"]  = round(float(prev["High"]), 2)
            result["prev_low"]   = round(float(prev["Low"]), 2)
            result["prev_close"] = round(float(prev["Close"]), 2)

        if not hist_1h.empty:
            today_str  = datetime.now(tz=EST).strftime("%Y-%m-%d")
            today_bars = hist_1h[
                hist_1h.index.tz_convert(EST).strftime("%Y-%m-%d") == today_str
            ]
            if not today_bars.empty:
                result["overnight_high"] = round(float(today_bars["High"].max()), 2)
                result["overnight_low"]  = round(float(today_bars["Low"].min()), 2)

        info       = ticker.fast_info
        price      = round(float(info.last_price), 2)
        prev_close = result["prev_close"] or round(float(info.previous_close), 2)
        change     = round(price - prev_close, 2)
        result.update({
            "price":      price,
            "change":     change,
            "change_pct": round((change / prev_close) * 100, 2) if prev_close else None,
            "prev_close": prev_close,
        })
    except Exception as e:
        logger.error(f"NQ full data error: {e}")
    return result


def get_vix() -> dict:
    """Fetch VIX level and classify volatility."""
    result = {"level": None, "rating": "unknown"}
    try:
        level = round(float(yf.Ticker("^VIX").fast_info.last_price), 2)
        if level < 15:
            rating = "low"
        elif level < 20:
            rating = "normal"
        elif level < 25:
            rating = "elevated"
        elif level < 30:
            rating = "high"
        else:
            rating = "extreme"
        result.update({"level": level, "rating": rating})
    except Exception as e:
        logger.error(f"VIX fetch error: {e}")
    return result


def get_economic_events() -> list[dict]:
    """
    Fetch high-impact USD economic events for this week from Finnhub.
    Falls back to empty list if key missing or request fails.
    """
    if not FINNHUB_API_KEY:
        return []
    try:
        now       = datetime.now(tz=EST)
        week_end  = now + timedelta(days=(6 - now.weekday()))
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"token": FINNHUB_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        events = r.json().get("economicCalendar", [])
        filtered = [
            e for e in events
            if e.get("country", "").upper() == "US"
            and e.get("impact", "").lower() in ("high", "medium")
            and e.get("time", "") >= now.strftime("%Y-%m-%d")
            and e.get("time", "") <= week_end.strftime("%Y-%m-%d")
        ]
        return sorted(filtered, key=lambda x: x.get("time", ""))[:6]
    except Exception as e:
        logger.error(f"Economic calendar error: {e}")
        return []


def get_earnings_this_week() -> list[str]:
    """
    Fetch earnings for NQ-relevant tech names this week from Finnhub.
    """
    if not FINNHUB_API_KEY:
        return []
    try:
        now      = datetime.now(tz=EST)
        from_dt  = now.strftime("%Y-%m-%d")
        to_dt    = (now + timedelta(days=(4 - now.weekday()) % 7 + 1)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": from_dt, "to": to_dt, "token": FINNHUB_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("earningsCalendar", [])
        relevant = [
            f"{e['symbol']} reports on {e['date']}"
            for e in items
            if e.get("symbol", "").upper() in NQ_TECH_NAMES
        ]
        return relevant[:5]
    except Exception as e:
        logger.error(f"Earnings calendar error: {e}")
        return []


def get_market_news(n: int = 3) -> list[str]:
    """Top N market/finance headlines."""
    if NEWSAPI_KEY:
        try:
            r = requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "category": "business",
                    "language": "en",
                    "pageSize": n,
                    "apiKey":   NEWSAPI_KEY,
                },
                timeout=10,
            )
            r.raise_for_status()
            articles = r.json().get("articles", [])
            headlines = [a["title"] for a in articles if a.get("title")][:n]
            if headlines:
                return headlines
        except Exception as e:
            logger.error(f"NewsAPI market error: {e}")

    # Finnhub fallback
    if FINNHUB_API_KEY:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/news",
                params={"category": "general", "token": FINNHUB_API_KEY},
                timeout=10,
            )
            r.raise_for_status()
            return [a["headline"] for a in r.json()[:n] if a.get("headline")]
        except Exception as e:
            logger.error(f"Finnhub news error: {e}")

    return []


def get_world_news(n: int = 2, exclude: list[str] | None = None) -> list[str]:
    """
    Top N general world headlines, deduped against the exclude list
    so market and world sections never surface the same headline.
    """
    exclude_set = {h.lower() for h in (exclude or [])}

    if NEWSAPI_KEY:
        try:
            r = requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "language": "en",
                    "pageSize": n + 10,   # fetch extra to absorb duplicates
                    "apiKey":   NEWSAPI_KEY,
                },
                timeout=10,
            )
            r.raise_for_status()
            articles  = r.json().get("articles", [])
            headlines = [
                a["title"] for a in articles
                if a.get("title") and a["title"].lower() not in exclude_set
            ][:n]
            if headlines:
                return headlines
        except Exception as e:
            logger.error(f"NewsAPI world error: {e}")

    # BBC RSS fallback
    try:
        r = requests.get("https://feeds.bbci.co.uk/news/rss.xml", timeout=10)
        r.raise_for_status()
        root  = ET.fromstring(r.content)
        items = root.findall(".//item/title")
        return [
            item.text for item in items
            if item.text and item.text.lower() not in exclude_set
        ][:n]
    except Exception as e:
        logger.error(f"BBC RSS error: {e}")

    return ["World news unavailable."]


def get_topic_news(query: str, n: int = 2) -> list[str]:
    """Search NewsAPI for a specific topic. Returns empty list if key missing."""
    if not NEWSAPI_KEY:
        return []
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":        query,
                "language": "en",
                "sortBy":   "publishedAt",
                "pageSize": n,
                "apiKey":   NEWSAPI_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])
        return [a["title"] for a in articles if a.get("title")][:n]
    except Exception as e:
        logger.error(f"Topic news error ({query}): {e}")
        return []


def get_nova_status() -> dict:
    """Ping the NOVA webhook server status endpoint."""
    try:
        r = requests.get(f"{NOVA_SERVER_URL}/status", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"NOVA server status error: {e}")
        return {}


def get_session_countdown() -> str:
    """Return a natural-language countdown to the next trading session."""
    now      = datetime.now(tz=EST)
    # Scope: London + NY AM only (see feedback memory "Trading scope")
    sessions = [
        ("London", now.replace(hour=2,  minute=0,  second=0, microsecond=0)),
        ("NY AM",  now.replace(hour=8,  minute=30, second=0, microsecond=0)),
    ]
    upcoming = []
    for name, t in sessions:
        if t <= now:
            t += timedelta(days=1)
        upcoming.append((name, t))

    next_name, next_time = min(upcoming, key=lambda x: x[1])
    delta   = next_time - now
    hours   = int(delta.total_seconds()) // 3600
    minutes = (int(delta.total_seconds()) % 3600) // 60

    if hours > 0:
        return (
            f"{next_name} session opens in {hours} hour{'s' if hours != 1 else ''} "
            f"and {minutes} minute{'s' if minutes != 1 else ''}."
        )
    return f"{next_name} session opens in {minutes} minute{'s' if minutes != 1 else ''}."


def get_market_conditions_rating(vix: dict, nq: dict) -> str:
    """Combine VIX and NQ movement into a plain-English conditions rating."""
    if not vix.get("level") or not nq.get("change_pct"):
        return "Data unavailable — assess conditions manually."

    rating = vix["rating"]
    move   = abs(nq["change_pct"])

    if rating in ("low", "normal") and move < 1.0:
        return "Excellent. Clean trend conditions expected. NOVA is clear to run."
    elif rating in ("low", "normal") and move < 2.0:
        return "Good. Normal conditions. Stay patient and let setups come to you."
    elif rating == "elevated" or move >= 2.0:
        return "Moderate. Elevated volatility. Respect your levels and stay mechanical."
    elif rating == "high":
        return "Challenging. High volatility. Reduce size and only take A-plus setups."
    else:
        return "Extreme conditions. Protect capital. Sit on hands unless setup is perfect."


def _vix_guidance(rating: str) -> str:
    return {
        "low":      "Conditions are calm. Standard risk rules apply.",
        "normal":   "Conditions are normal. Proceed as planned.",
        "elevated": "Volatility is elevated. Stay disciplined on entries.",
        "high":     "High volatility. Consider reducing size or standing aside.",
        "extreme":  "Extreme volatility. Protect capital first, Sir.",
    }.get(rating, "")


# ── Mindset Assessment ─────────────────────────────────────────────────────────

def assess_mindset(energy: int, q2_text: str, q3_text: str) -> tuple[str, str]:
    """
    Returns (clearance_level, spoken_message).
    clearance_level: "clear" | "caution" | "sit_out"
    """
    stress_words  = {
        "stressed", "tired", "exhausted", "worried", "anxious",
        "sick", "distracted", "scattered", "rough", "bad", "upset",
        "argument", "fight", "problem", "issue",
    }
    revenge_words = {
        "recover", "revenge", "back", "make up", "make back",
        "yesterday", "negative", "owe", "lost", "loss",
    }

    has_stress  = any(w in q2_text for w in stress_words) or (
        "yes" in q2_text and len(q2_text) < 20
    )
    has_revenge = any(w in q3_text for w in revenge_words)

    if energy >= 8 and not has_stress and not has_revenge:
        return "clear", (
            f"Energy at {energy} out of 10. No distractions. Clean intent. "
            "You are clear to trade, Sir. Lock in, trust the system, and let NOVA work."
        )
    elif energy >= 5 and not has_revenge:
        return "caution", (
            f"Energy at {energy} out of 10. "
            + ("Some stress present. " if has_stress else "")
            + "You are functional but not at your peak. "
            "Stay mechanical today, Sir. No discretionary entries. "
            "Let NOVA handle the signals and do not override it."
        )
    else:
        reasons = []
        if energy < 5:
            reasons.append(f"energy is low at {energy} out of 10")
        if has_stress:
            reasons.append("stress is present")
        if has_revenge:
            reasons.append("revenge trading pattern detected")
        reason_str = ", and ".join(reasons)
        return "sit_out", (
            f"Sir, {reason_str}. "
            "I am recommending you sit out today's session. "
            "Protect the account. The market will be here tomorrow. "
            "There will be better setups when you are operating at full capacity."
        )


# ── Daily Focus Generator ──────────────────────────────────────────────────────

def get_daily_focus(vix_rating: str) -> tuple[list[str], str, str]:
    """Returns (top_3_priorities, thing_to_avoid, discipline_reminder)."""
    now = datetime.now(tz=EST)
    day = now.weekday()  # 0=Monday, 4=Friday

    day_priorities = {
        0: [
            "Mark weekly high and low targets on the NQ chart",
            "Review the full week economic calendar for high-impact events",
            "Confirm all TradingView alerts are active for the NY AM session",
        ],
        1: [
            "Review Monday's price action and update key levels",
            "Focus on NY AM — Tuesday tends to be the highest volume day",
            "Log any open observations from yesterday in Obsidian",
        ],
        2: [
            "Mid-week check — are you up or down on the week?",
            "FOMC days tend to fall on Wednesdays — check the calendar",
            "Stay patient — Wednesdays can be choppy ahead of announcements",
        ],
        3: [
            "Review week-to-date performance before adding any new risk",
            "Update key levels from Wednesday's range",
            "KeepLodge — check for new waitlist submissions and follow-ups",
        ],
        4: [
            "Friday is a liquidity hunt day — be cautious of stop runs",
            "Do not add new risk on Fridays — protect the weekly gain",
            "End of week review — log the week in Obsidian before close",
        ],
    }
    priorities = day_priorities.get(day, [
        "Mark key levels before the session opens",
        "Confirm NOVA server status and TradingView alerts",
        "Log trades same day — do not let them stack up",
    ])

    avoid_map = {
        "low":      "Avoid overtrading in low volatility — wait for the real move to develop",
        "normal":   "Avoid chasing entries — let price come to your levels",
        "elevated": "Avoid widening stops to accommodate volatility — reduce size instead",
        "high":     "Avoid taking trades without clear confirmation — high VIX creates traps",
        "extreme":  "Avoid trading at all unless the setup is A-plus — capital protection first",
    }
    thing_to_avoid = avoid_map.get(vix_rating, "Avoid any entry that is not on your plan")

    reminders = [
        "One trade at a time. Let the first trade breathe before thinking about the next.",
        "Your job is to execute the system, not predict the market.",
        "A loss taken quickly is a win. A loss held too long is a disaster.",
        "The market will be here tomorrow. Capital management is everything.",
        "NOVA gives the signal. Your only job is to not override it.",
        "Patience is a position. Waiting for the right setup is a trade in itself.",
        "Size is risk. When in doubt, go smaller.",
    ]
    discipline_reminder = reminders[now.day % len(reminders)]

    return priorities, thing_to_avoid, discipline_reminder


# ── Morning Briefing ───────────────────────────────────────────────────────────

def morning_briefing():
    """Full 8-section 8:00am EST morning briefing."""
    now = datetime.now(tz=EST)
    logger.info("Running morning briefing")

    speak(
        f"Good morning Sir. NOVA morning briefing for "
        f"{now.strftime('%A, %B %d, %Y')}. "
        f"Time is {now.strftime('%I:%M %p')} Eastern. "
        "Stand by for your full pre-flight."
    )

    # ── SECTION 0 — RECALL FROM NEURAL BRAIN ──────────────────────────────────
    # NOVA now opens the day by surfacing the top insight from the last session.
    if _BRAIN_ENABLED:
        try:
            yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            hits = _brain_search(f"debrief {yday}", limit=2) or _brain_search(
                "yesterday trade session loss setup", limit=2
            )
            if hits:
                top = hits[0]
                summary = top.get("summary") or (top.get("content") or "")[:180]
                if summary:
                    speak("Recalling yesterday.")
                    speak(summary)
        except Exception as _e:
            logger.debug(f"brain recall skipped: {_e}")

    # ── SECTION 1 — TIME AND WEATHER ──────────────────────────────────────────
    speak("Section 1. Time and weather.")

    weather = get_weather()
    if weather:
        speak(
            f"Toronto. {weather['desc']}. Currently {weather['temp_c']} degrees, "
            f"feels like {weather['feels_c']}. "
            f"Today's range is {weather['min_c']} to {weather['max_c']} degrees. "
            f"Humidity at {weather['humidity']} percent."
        )
    else:
        speak("Toronto weather data unavailable.")

    # ── SECTION 2 — MARKET OVERVIEW ───────────────────────────────────────────
    speak("Section 2. Market overview.")

    nq  = get_nq_full_data()
    vix = get_vix()

    # NQ price and overnight range
    if nq.get("price"):
        direction = "up" if (nq["change"] or 0) >= 0 else "down"
        speak(
            f"NQ futures are currently trading at {nq['price']}, "
            f"{direction} {abs(nq['change'])} points — "
            f"{abs(nq['change_pct'])} percent from yesterday's close of {nq['prev_close']}."
        )
        if nq.get("overnight_high") and nq.get("overnight_low"):
            speak(
                f"Overnight range: high of {nq['overnight_high']}, "
                f"low of {nq['overnight_low']}. "
                f"That is a {round(nq['overnight_high'] - nq['overnight_low'], 2)} point range."
            )
        if nq.get("prev_high") and nq.get("prev_low"):
            speak(
                f"Key levels to watch. "
                f"Previous day high at {nq['prev_high']}, "
                f"previous day low at {nq['prev_low']}, "
                f"previous close at {nq['prev_close']}. "
                "Mark these on your chart before the session opens."
            )
    else:
        speak("NQ futures data unavailable at this time.")

    # VIX
    if vix.get("level"):
        speak(
            f"VIX is at {vix['level']}. Volatility is {vix['rating']}. "
            f"{_vix_guidance(vix['rating'])}"
        )
    else:
        speak("VIX data unavailable.")

    # Economic calendar
    events = get_economic_events()
    if events:
        speak("Major economic events this week.")
        for e in events:
            impact = e.get("impact", "").capitalize()
            speak(f"{e.get('event', 'Event')} on {e.get('time', '')}. Impact: {impact}.")
    else:
        speak(
            "No economic calendar data available. "
            "Check ForexFactory or the Finnhub calendar manually, Sir."
        )

    # Earnings
    earnings = get_earnings_this_week()
    if earnings:
        speak("NQ-relevant earnings this week.")
        for e in earnings:
            speak(e)
    else:
        speak("No major NQ-relevant earnings flagged for this week.")

    # ── SECTION 3 — NEWS ──────────────────────────────────────────────────────
    speak("Section 3. News briefing.")

    market_news = get_market_news(3)
    if market_news:
        speak("Top market headlines.")
        for i, h in enumerate(market_news, 1):
            speak(f"{i}. {h}")
    else:
        speak("Market headlines unavailable.")

    world_news = get_world_news(2, exclude=market_news)
    if world_news:
        speak("Top world headlines.")
        for i, h in enumerate(world_news, 1):
            speak(f"{i}. {h}")

    prop_news = get_topic_news(
        '"prop firm" AND (funded trader OR FTMO OR Topstep OR evaluation account OR futures trader)', 2
    )
    if prop_news:
        speak("Prop firm industry news.")
        for i, h in enumerate(prop_news, 1):
            speak(f"{i}. {h}")
    else:
        speak("No prop firm industry news detected today.")

    str_news = get_topic_news("short term rental regulation Airbnb", 2)
    if str_news:
        speak("Short term rental news relevant to KeepLodge.")
        for i, h in enumerate(str_news, 1):
            speak(f"{i}. {h}")
    else:
        speak("No short term rental regulation news flagged today.")

    # ── SECTION 4 — ACCOUNT STATUS ────────────────────────────────────────────
    speak("Section 4. Account status.")
    speak(
        "Evaluation account equity — data connection pending. "
        "3 accounts active. Manual check required in Tradovate, Sir."
    )
    speak("Payout targets — progress tracking not yet automated. Check each account dashboard.")
    speak("Yesterday's P and L — not yet connected. Log in Obsidian after each session.")
    speak("Monthly Whop revenue — dashboard check required.")
    speak("Monthly Discord revenue — dashboard check required.")

    # ── SECTION 5 — BUSINESS PULSE ────────────────────────────────────────────
    speak("Section 5. Business pulse.")

    if _WAITLIST_ENABLED:
        try:
            report = _waitlist_report()
            total, today_count = "unknown", "unknown"
            for line in report.splitlines():
                if "Total signups:" in line:
                    total = line.split(":", 1)[1].strip()
                elif "New today:" in line:
                    today_count = line.split(":", 1)[1].strip()
            speak(
                f"KeepLodge waitlist. {total} total signups. "
                f"{today_count} new since midnight."
            )
        except Exception as e:
            logger.error(f"Waitlist report error: {e}")
            speak("KeepLodge waitlist data unavailable.")
    else:
        speak("KeepLodge waitlist — automated count not yet connected. Check dashboard manually.")

    speak("New Discord members overnight — manual check required in server dashboard.")

    if _COMPETITIVE_ENABLED:
        try:
            comp_summary = _competitive_summary()
            if comp_summary:
                speak(f"Competitive intel. {comp_summary}")
        except Exception as e:
            logger.error(f"Competitive intel error: {e}")

    speak("Urgent tasks from yesterday — check your Obsidian task list, Sir.")

    # ── SECTION 6 — NOVA READINESS ────────────────────────────────────────────
    speak("Section 6. NOVA system readiness.")

    nova = get_nova_status()
    if nova:
        trades    = nova.get("trades_today", 0)
        loss      = nova.get("daily_loss", 0.0)
        remaining = nova.get("loss_remaining", 500.0)
        active    = nova.get("active_session", "None")
        speak(
            f"NOVA webhook server is online. "
            f"Active session: {active}. "
            f"{trades} of 3 trades taken today. "
            f"Daily loss at {loss:.0f} dollars. "
            f"{remaining:.0f} dollars of risk budget remaining."
        )
    else:
        speak(
            "NOVA server is not responding. "
            "Please check the Railway deployment before arming up, Sir."
        )

    speak(get_session_countdown())

    conditions_msg = get_market_conditions_rating(vix, nq)
    speak(f"Market conditions rating for today. {conditions_msg}")

    # ── SECTION 7 — MINDSET CHECK-IN ─────────────────────────────────────────
    speak(
        "Section 7. Mindset check-in. "
        "Three questions, Sir. Take your time with each one."
    )
    time.sleep(1)

    speak("Question one. How are you feeling today on a scale of 1 to 10?")
    q1_text = listen_response(timeout=15)
    if not q1_text:
        time.sleep(10)  # fallback pause if mic unavailable

    numbers = re.findall(r'\b(10|[1-9])\b', q1_text)
    energy  = int(numbers[0]) if numbers else 5
    if q1_text:
        speak(f"Noted, Sir. {energy} out of 10.")

    speak("Question two. Any distractions or stress I should know about?")
    q2_text = listen_response(timeout=15)
    if not q2_text:
        time.sleep(12)

    speak("Question three. Are you trading to make money today, or are you trying to recover something?")
    q3_text = listen_response(timeout=15)
    if not q3_text:
        time.sleep(12)

    clearance, assessment_msg = assess_mindset(energy, q2_text, q3_text)
    speak(assessment_msg)

    if clearance == "sit_out":
        speak(
            "The briefing is complete. No trading today. "
            "Use the time to review charts, update Obsidian, or rest. "
            "I will be here when you are ready, Sir."
        )
        return  # End briefing early if sitting out

    # ── SECTION 8 — DAILY FOCUS ───────────────────────────────────────────────
    speak("Section 8. Daily focus.")

    priorities, thing_to_avoid, discipline_reminder = get_daily_focus(
        vix.get("rating", "normal")
    )

    speak("Your top 3 priorities for today.")
    for i, p in enumerate(priorities, 1):
        speak(f"{i}. {p}")

    speak(f"One thing to avoid today. {thing_to_avoid}")
    speak(f"Discipline reminder. {discipline_reminder}")

    speak(
        "Briefing complete. "
        "Lock in, stay mechanical, and let the system work. "
        "Let's have a great session, Sir."
    )

    # Store briefing summary to Neural Brain via the structured helper —
    # auto-tags with `nova:briefing:<date>` heading so it's queryable later.
    if _BRAIN_ENABLED:
        try:
            date_str = now.strftime('%Y-%m-%d')
            nq_price = nq.get('price', 'N/A') if nq else 'N/A'
            vix_level = vix.get('level', 'N/A') if vix else 'N/A'
            vix_rating = vix.get('rating', 'unknown') if vix else 'unknown'
            brief_content = (
                f"Morning briefing {date_str}.\n"
                f"Energy: {energy}/10. Mindset clearance: {clearance}.\n"
                f"NQ: {nq_price}. VIX: {vix_level} ({vix_rating}).\n"
                f"Mindset check-in: {q2_text[:140] if q2_text else 'no response'}.\n"
                f"Priorities: {', '.join(priorities[:3]) if priorities else 'none set'}.\n"
                f"Avoid today: {thing_to_avoid}.\n"
                f"Discipline: {discipline_reminder}."
            )
            _brain_remember_briefing(
                date_str,
                brief_content,
                summary=f"Morning brief {date_str} — energy {energy}/10, VIX {vix_rating}, clearance {clearance}",
            )
        except Exception as _e:
            logger.debug(f"brain briefing store failed: {_e}")


# ── Waitlist Poll Loop ─────────────────────────────────────────────────────────

def waitlist_poll_loop():
    """Background thread: poll all KeepLodge waitlist forms every 5 minutes."""
    while True:
        try:
            new_count = _waitlist_poll()
            for _ in range(new_count):
                speak("Sir, a new host has joined the KeepLodge waitlist.")
        except Exception as e:
            logger.error(f"Waitlist poll error: {e}")
        time.sleep(300)


# ── Timed Alerts ───────────────────────────────────────────────────────────────

def alert_arms_up():
    speak(
        "Sir, it is 8:15 AM. NOVA trading system arms up. "
        "Confirm the Railway server is live and TradingView alerts are active."
    )


def alert_ny_session_5min():
    speak(
        "Sir, NY session opens in 5 minutes. "
        "Get to your desk. 8:30 AM is incoming."
    )


def alert_nyse_5min():
    speak(
        "Sir, NYSE opens in 5 minutes. 9:30 AM. "
        "Watch for volume expansion and volatility spikes on the open."
    )


def alert_session_closed():
    speak(
        "Sir, it is 11:00 AM. NY morning session is now closed. "
        "Step away from the screen. No more trades until the next session."
    )


def eod_debrief():
    """4:00pm EST end-of-day debrief."""
    now  = datetime.now(tz=EST)
    nova = get_nova_status()

    speak(f"Good evening Sir. End of day debrief for {now.strftime('%A, %B %d')}.")

    if nova:
        trades    = nova.get("trades_today", 0)
        loss      = nova.get("daily_loss", 0.0)
        remaining = nova.get("loss_remaining", 500.0)
        speak(
            f"Today's summary. "
            f"{trades} trade{'s' if trades != 1 else ''} taken. "
            f"Daily loss standing at {loss:.0f} dollars. "
            f"{remaining:.0f} dollars of risk budget remaining."
        )
    else:
        speak("NOVA server data unavailable for debrief.")

    speak(
        "Log your trades in Obsidian. "
        "Review your entries, exits, and emotional state. "
        "What did you learn today, Sir?"
    )
    speak("Rest well. Tomorrow we go again.")

    # Store debrief to Neural Brain — becomes queryable for tomorrow's briefing
    if _BRAIN_ENABLED:
        try:
            date_str = now.strftime('%Y-%m-%d')
            trades    = nova.get('trades_today', 0)    if nova else 0
            loss      = nova.get('daily_loss', 0.0)    if nova else 0.0
            remaining = nova.get('loss_remaining', 500.0) if nova else 500.0
            session_trades = nova.get('session_trades', {}) if nova else {}
            body = (
                f"EOD debrief {date_str}.\n"
                f"Trades today: {trades}.\n"
                f"Daily loss: ${loss:.0f}.\n"
                f"Risk budget remaining: ${remaining:.0f}.\n"
                f"Per-session: {session_trades}."
            )
            _brain_remember_debrief(
                date_str,
                body,
                summary=f"EOD {date_str} — {trades} trades, ${loss:.0f} loss, ${remaining:.0f} remaining",
            )
        except Exception as _e:
            logger.debug(f"brain debrief store failed: {_e}")


# ── Wake Word Listener ─────────────────────────────────────────────────────────

def _dispatch_command_action(response, utterance: str):
    """
    Execute the structured action returned by the Claude-powered classifier.
    Called after NOVA has already spoken the `spoken` reply.
    """
    action = getattr(response, "action", "UNKNOWN")

    if action == "STATUS":
        try:
            nova = get_nova_status()
            if not nova:
                speak("Status is offline, Sir.")
                return
            trades    = nova.get("trades_today", 0)
            loss      = nova.get("daily_loss", 0.0)
            remaining = nova.get("loss_remaining", 500.0)
            session   = nova.get("active_session") or "none"
            speak(
                f"Session: {session}. {trades} trades today. "
                f"Daily loss {loss:.0f}. Risk budget {remaining:.0f} remaining."
            )
        except Exception as e:
            logger.error(f"STATUS dispatch failed: {e}")
            speak("Couldn't pull status, Sir.")
        return

    if action == "MORNING_BRIEF":
        threading.Thread(target=morning_briefing, daemon=True).start()
        return

    if action == "DEBRIEF":
        threading.Thread(target=eod_debrief, daemon=True).start()
        return

    if action == "REMEMBER":
        payload = (getattr(response, "payload", "") or utterance).strip()
        if _COMMAND_AI_ENABLED and payload:
            ok = _nova_cmd_remember(payload)
            if not ok:
                speak("Couldn't save that to the Brain, Sir.")
        return

    if action == "RECALL":
        query = (getattr(response, "payload", "") or utterance).strip()
        if _COMMAND_AI_ENABLED and query:
            recap = _nova_cmd_recall(query, limit=3)
            if recap:
                speak(recap)
        return

    if action == "LEVELS":
        # /levels skill is Claude-driven; no inline implementation yet.
        speak("Pull up the chart. I'll narrate levels when the /levels skill is wired in, Sir.")
        return

    if action == "PATTERN":
        speak("Running the pattern agent — this may take a moment.")
        try:
            import subprocess
            subprocess.Popen(
                ["python", os.path.join(os.path.dirname(os.path.abspath(__file__)), "nova_pattern_agent.py")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error(f"PATTERN dispatch failed: {e}")
        return

    if action == "REFLECT":
        # Fire a one-off reflection via the Neural Brain backend
        try:
            import requests as _rq
            _rq.post("http://127.0.0.1:7337/insights/run", timeout=5)
            speak("Reflection running. Check the Brain for new insights shortly.")
        except Exception:
            speak("Brain is offline. Can't reflect without it, Sir.")
        return

    # CHAT / UNKNOWN — the spoken reply is all we need
    return


def listen_for_wake_word():
    """
    Continuously listen for the wake phrase, then route the follow-up command
    through the Claude-powered classifier (nova_command_ai). Falls back to
    keyword matching if Claude / Brain is unavailable, so voice never dies.
    """
    recognizer = sr.Recognizer()
    logger.info(f"Wake word listener active. Say: '{WAKE_PHRASE}'")
    logger.info(f"Command AI: {'ON' if _COMMAND_AI_ENABLED else 'OFF (fallback keyword matcher)'}")

    while True:
        try:
            audio = _capture_audio(4.0)
            text  = recognizer.recognize_google(audio).lower()
            logger.info(f"Heard: {text}")

            if WAKE_PHRASE not in text:
                continue

            logger.info("Wake word detected")
            speak("Sir.")

            # Capture the command utterance
            command = listen_response(timeout=8)
            if not command:
                speak("Didn't catch that, Sir.")
                continue
            logger.info(f"Command utterance: {command}")

            # Classify via Claude + Brain context (or fallback)
            if _COMMAND_AI_ENABLED:
                response = _nova_classify(command)
                logger.info(
                    f"Action={response.action} | "
                    f"Reasoning={response.reasoning}"
                )
                if response.spoken:
                    speak(response.spoken)
                _dispatch_command_action(response, command)
            else:
                # Pure fallback — no Claude, no Brain — bare keyword dispatch
                cl = command.lower()
                if "status" in cl:
                    _dispatch_command_action(
                        type("R", (), {"action":"STATUS","payload":"","spoken":"","reasoning":""})(),
                        command,
                    )
                else:
                    speak("Command AI is offline, Sir. Start it to use voice.")

        except sr.UnknownValueError:
            pass   # silence or unintelligible — keep looping
        except sr.RequestError as e:
            logger.error(f"STT service error: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Wake word listener error: {e}")
            time.sleep(2)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def run_scheduler():
    """
    All times are system local time.
    Ensure this machine is set to EST/EDT (Toronto).
    """
    schedule.every().day.at("08:00").do(morning_briefing)
    schedule.every().day.at("08:15").do(alert_arms_up)
    schedule.every().day.at("08:25").do(alert_ny_session_5min)
    schedule.every().day.at("09:25").do(alert_nyse_5min)
    schedule.every().day.at("11:00").do(alert_session_closed)
    schedule.every().day.at("16:00").do(eod_debrief)

    logger.info("Scheduler armed:")
    logger.info("  08:00 — Morning briefing (8 sections)")
    logger.info("  08:15 — Arms up")
    logger.info("  08:25 — NY session in 5 minutes")
    logger.info("  09:25 — NYSE opens in 5 minutes")
    logger.info("  11:00 — Session closed")
    logger.info("  16:00 — EOD debrief")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    speak(
        "NOVA Assistant online. Good morning Sir. "
        f"Say '{WAKE_PHRASE}' to activate me at any time."
    )

    scheduler_thread = threading.Thread(
        target=run_scheduler, daemon=True, name="nova-scheduler"
    )
    scheduler_thread.start()

    if _WAITLIST_ENABLED:
        waitlist_thread = threading.Thread(
            target=waitlist_poll_loop, daemon=True, name="nova-waitlist"
        )
        waitlist_thread.start()
        logger.info("Waitlist agent armed — polling every 5 minutes.")
    else:
        logger.warning("Waitlist agent not loaded — keeplodge/waitlist_agent.py not found.")

    # Strategy drift monitor — background thread watching win-rate / streaks / drawdown
    try:
        from nova_drift_monitor import DriftMonitor
        DriftMonitor(speaker=speak).start()
        logger.info("Drift monitor armed — checking every 60 minutes.")
    except Exception as _e:
        logger.warning(f"Drift monitor failed to start: {_e}")

    listen_for_wake_word()


if __name__ == "__main__":
    main()
