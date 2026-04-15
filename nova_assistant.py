#!/usr/bin/env python3
"""
NOVA Assistant — Personal AI voice assistant for Sir
Wake word : "alright nova lets cook"
Scheduled : 8:00am morning briefing + timed session alerts (EST)
Voice     : ElevenLabs Daniel British  (ID: 766NdLzxBMJanRvWXtkt)
"""

import logging
import os
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pygame
import requests
import schedule
import speech_recognition as sr
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = "766NdLzxBMJanRvWXtkt"          # Daniel British
FINNHUB_API_KEY     = os.environ.get("FINNHUB_API_KEY", "")
NEWSAPI_KEY         = os.environ.get("NEWSAPI_KEY", "")
NOVA_SERVER_URL     = os.environ.get(
    "NOVA_SERVER_URL", "https://nova-production-72f5.up.railway.app"
)
WAKE_PHRASE = "alright nova lets cook"
EST         = ZoneInfo("America/New_York")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("nova_assistant.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ── Text-to-Speech ─────────────────────────────────────────────────────────────

def speak(text: str):
    """Convert text to speech via ElevenLabs. Falls back to print if key missing."""
    logger.info(f"[NOVA]: {text}")

    if not ELEVENLABS_API_KEY:
        print(f"\n[NOVA]: {text}\n")
        return

    try:
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=20,
        )
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(response.content)
            tmp_path = f.name

        pygame.mixer.init()
        pygame.mixer.music.load(tmp_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)
        pygame.mixer.quit()
        os.unlink(tmp_path)

    except requests.exceptions.RequestException as e:
        logger.error(f"ElevenLabs request error: {e}")
        print(f"\n[NOVA]: {text}\n")
    except Exception as e:
        logger.error(f"TTS playback error: {e}")
        print(f"\n[NOVA]: {text}\n")


# ── Data Fetchers ──────────────────────────────────────────────────────────────

def get_weather() -> str:
    """Fetch Toronto weather from wttr.in (no API key required)."""
    try:
        r = requests.get("https://wttr.in/Toronto?format=j1", timeout=10)
        r.raise_for_status()
        current  = r.json()["current_condition"][0]
        temp_c   = current["temp_C"]
        feels_c  = current["FeelsLikeC"]
        desc     = current["weatherDesc"][0]["value"]
        humidity = current["humidity"]
        return (
            f"{desc}, {temp_c} degrees Celsius, feels like {feels_c}. "
            f"Humidity at {humidity} percent."
        )
    except Exception as e:
        logger.error(f"Weather fetch error: {e}")
        return "Weather data unavailable."


def get_news_headlines() -> list[str]:
    """
    World headlines — NewsAPI if NEWSAPI_KEY is set, otherwise BBC RSS fallback.
    """
    if NEWSAPI_KEY:
        try:
            r = requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={"language": "en", "pageSize": 5, "apiKey": NEWSAPI_KEY},
                timeout=10,
            )
            r.raise_for_status()
            articles = r.json().get("articles", [])
            headlines = [a["title"] for a in articles if a.get("title")][:5]
            if headlines:
                return headlines
        except Exception as e:
            logger.error(f"NewsAPI error: {e}")

    # BBC RSS fallback
    try:
        r = requests.get("https://feeds.bbci.co.uk/news/rss.xml", timeout=10)
        r.raise_for_status()
        root  = ET.fromstring(r.content)
        items = root.findall(".//item/title")
        return [item.text for item in items if item.text][:5]
    except Exception as e:
        logger.error(f"BBC RSS error: {e}")

    return ["News headlines unavailable."]


def get_market_headlines() -> list[str]:
    """Top market headlines from Finnhub (requires FINNHUB_API_KEY)."""
    if not FINNHUB_API_KEY:
        return []
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": FINNHUB_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        return [a["headline"] for a in r.json()[:3] if a.get("headline")]
    except Exception as e:
        logger.error(f"Finnhub market news error: {e}")
        return []


def get_nq_data() -> dict:
    """
    NQ futures overnight data.
    Tries Finnhub (NQ:GLOBEX) first, falls back to yfinance (NQ=F).
    """
    result = {"price": None, "change": None, "change_pct": None}

    if FINNHUB_API_KEY:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": "NQ:GLOBEX", "token": FINNHUB_API_KEY},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("c"):
                result.update({
                    "price":      round(data["c"], 2),
                    "change":     round(data["d"], 2),
                    "change_pct": round(data["dp"], 2),
                })
                return result
        except Exception as e:
            logger.error(f"Finnhub NQ error: {e}")

    # yfinance fallback
    try:
        info       = yf.Ticker("NQ=F").fast_info
        price      = round(info.last_price, 2)
        prev_close = round(info.previous_close, 2)
        change     = round(price - prev_close, 2)
        result.update({
            "price":      price,
            "change":     change,
            "change_pct": round((change / prev_close) * 100, 2),
        })
    except Exception as e:
        logger.error(f"yfinance NQ error: {e}")

    return result


def get_vix() -> dict:
    """Fetch VIX level and classify volatility."""
    result = {"level": None, "rating": "unknown"}
    try:
        level = round(yf.Ticker("^VIX").fast_info.last_price, 2)
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
    now = datetime.now(tz=EST)
    sessions = [
        ("Asia",   now.replace(hour=19, minute=0,  second=0, microsecond=0)),
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


def _vix_guidance(rating: str) -> str:
    return {
        "low":      "Conditions are calm. Standard risk rules apply.",
        "normal":   "Conditions are normal. Proceed as planned.",
        "elevated": "Volatility is elevated. Stay disciplined on entries.",
        "high":     "High volatility. Consider reducing size or standing aside.",
        "extreme":  "Extreme volatility. Protect capital first, Sir.",
    }.get(rating, "")


# ── Morning Briefing ───────────────────────────────────────────────────────────

def morning_briefing():
    """Full 8:00am EST morning briefing."""
    now = datetime.now(tz=EST)
    logger.info("Running morning briefing")

    speak(
        f"Good morning Sir. It is {now.strftime('%A, %B %d, %Y')} "
        f"and the time is {now.strftime('%I:%M %p')} Eastern."
    )

    # Weather
    speak(f"Toronto weather. {get_weather()}")

    # World news
    speak("Top world headlines.")
    for i, h in enumerate(get_news_headlines(), 1):
        speak(f"{i}. {h}")

    # Market news
    market = get_market_headlines()
    if market:
        speak("Market news.")
        for i, h in enumerate(market, 1):
            speak(f"{i}. {h}")

    # NQ futures
    nq = get_nq_data()
    if nq["price"]:
        direction = "up" if nq["change"] >= 0 else "down"
        speak(
            f"NQ futures overnight. Currently at {nq['price']}, "
            f"{direction} {abs(nq['change'])} points, "
            f"{abs(nq['change_pct'])} percent from yesterday's close."
        )
    else:
        speak("NQ futures data unavailable.")

    # VIX
    vix = get_vix()
    if vix["level"]:
        speak(
            f"VIX is at {vix['level']}. Volatility is {vix['rating']}. "
            f"{_vix_guidance(vix['rating'])}"
        )
    else:
        speak("VIX data unavailable.")

    # NOVA system readiness
    nova = get_nova_status()
    if nova:
        trades    = nova.get("trades_today", 0)
        loss      = nova.get("daily_loss", 0.0)
        remaining = nova.get("loss_remaining", 500.0)
        speak(
            f"NOVA trading system is online. "
            f"{trades} of 3 trades taken today. "
            f"Daily loss at {loss:.0f} of 500 dollars. "
            f"{remaining:.0f} dollars of risk budget remaining."
        )
    else:
        speak(
            "NOVA trading system status unavailable. "
            "Please check the Railway deployment, Sir."
        )

    # Session countdown
    speak(get_session_countdown())

    # KeepLodge placeholder
    speak(
        "KeepLodge waitlist update. Automated data not yet connected. "
        "Please check the dashboard manually, Sir."
    )

    # Daily tasks
    speak(
        "Your daily task list. "
        "One — review pre-market levels and mark key liquidity zones. "
        "Two — set trade alerts in TradingView. "
        "Three — confirm NOVA server is armed and webhook is live. "
        "Four — review yesterday's trades if any."
    )

    # Mindset check-in
    speak("Mindset check-in. Three questions, Sir.")
    time.sleep(1)
    speak("One — what is your intention for today's trading session?")
    time.sleep(12)
    speak("Two — are you trading from a place of clarity or emotion today?")
    time.sleep(12)
    speak("Three — what does a winning day look like for you right now?")
    time.sleep(12)
    speak("Lock it in. Let's have a great session, Sir.")


# ── Timed Alerts ───────────────────────────────────────────────────────────────

def alert_arms_up():
    speak(
        "Sir, it is 8:15 AM. NOVA trading system arms up. "
        "Confirm the server is live and your TradingView alerts are active."
    )


def alert_ny_session_5min():
    speak(
        "Sir, the NY session opens in 5 minutes. "
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
        "Review your entries, your exits, and your emotional state. "
        "What did you learn today, Sir?"
    )
    speak("Rest well. Tomorrow we go again.")


# ── Wake Word Listener ─────────────────────────────────────────────────────────

def listen_for_wake_word():
    """Continuously listen for the wake phrase and activate on detection."""
    recognizer = sr.Recognizer()
    recognizer.energy_threshold        = 3000
    recognizer.dynamic_energy_threshold = True
    mic = sr.Microphone()

    logger.info(f"Wake word listener active. Say: '{WAKE_PHRASE}'")

    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=2)

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
            pass  # silence — keep listening
        except sr.UnknownValueError:
            pass  # could not understand audio
        except sr.RequestError as e:
            logger.error(f"Speech recognition service error: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Wake word listener error: {e}")
            time.sleep(2)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def run_scheduler():
    """
    All times are system local time — ensure this machine is set to EST/EDT.
    Railway would need UTC offsets; this file is intended for local execution.
    """
    schedule.every().day.at("08:00").do(morning_briefing)
    schedule.every().day.at("08:15").do(alert_arms_up)
    schedule.every().day.at("08:25").do(alert_ny_session_5min)
    schedule.every().day.at("09:25").do(alert_nyse_5min)
    schedule.every().day.at("11:00").do(alert_session_closed)
    schedule.every().day.at("16:00").do(eod_debrief)

    logger.info("Scheduler armed:")
    logger.info("  08:00 — Morning briefing")
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

    # Wake word runs on the main thread
    listen_for_wake_word()


if __name__ == "__main__":
    main()
