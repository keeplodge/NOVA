"""
═══════════════════════════════════════════════════════════════════════════
NOVA News Agent — Discord posts for the Hunnid Ticks #economic-calendar
───────────────────────────────────────────────────────────────────────────

Pulls the Forex Factory weekly XML feed (free, no API key, ~15-year-stable),
filters USD high-impact events, and posts Discord embeds on schedule:

  - Weekly preview   : Sunday 18:00 EST + immediately on first boot
  - Daily menu       : Mon-Fri 07:00 EST
  - Pre-event alert  : 10 minutes before each event
  - Post-event result: when actual appears in the XML (poll every 5 min
                       during trading hours)

Runs as a daemon thread inside the Railway Flask app. Posts go to
NOVA_NEWS_DISCORD_WEBHOOK_URL (a DIFFERENT webhook than the signals one,
so signals + news go to different channels).

Idempotency: every embed has a stable event_id (date|time|title). Dedup
map prevents the same pre/post alert from firing twice.
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests


EST = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

FF_XML_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

logger = logging.getLogger("nova-news-agent")


# ═══════════════════════════════════════════════════════════════════════════
# NewsAgent
# ═══════════════════════════════════════════════════════════════════════════

class NewsAgent:
    """Forex Factory → Discord relay for #economic-calendar."""

    def __init__(self, discord_url: str | None = None):
        self.discord_url = discord_url or os.environ.get("NOVA_NEWS_DISCORD_WEBHOOK_URL", "")
        self._stop   = threading.Event()
        self._thread = None

        # Idempotency maps — each is date-stamped so they reset on day rollover
        self._fired_pre:     set[str]  = set()
        self._fired_post:    set[str]  = set()
        self._last_daily_at: datetime | None = None
        self._last_weekly_at: datetime | None = None
        self._events_cache:   list[dict] = []
        self._events_cached_at: datetime | None = None

    # ── Feed fetch + parse ───────────────────────────────────────────────
    def fetch_events(self, force: bool = False) -> list[dict]:
        """Returns cached events list. Refetch if cache >10 min old or forced."""
        now = datetime.now(tz=EST)
        if (not force
                and self._events_cached_at
                and (now - self._events_cached_at).total_seconds() < 600
                and self._events_cache):
            return self._events_cache
        try:
            r = requests.get(FF_XML_URL, timeout=10,
                             headers={"User-Agent": "NOVA News Agent / 1.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            events = []
            for e in root.findall("event"):
                evt = {t.tag: (t.text or "").strip() for t in e}
                events.append(evt)
            self._events_cache      = events
            self._events_cached_at  = now
            logger.info(f"fetched {len(events)} events from Forex Factory")
            return events
        except Exception as ex:
            logger.error(f"FF fetch error: {ex}")
            return self._events_cache  # fall back to stale cache

    def parse_event_time(self, evt: dict) -> datetime | None:
        """Parse MM-DD-YYYY + H:MMam|pm → EST datetime. None if unparseable."""
        d = evt.get("date", "")
        t = evt.get("time", "")
        if not d or not t:
            return None
        if t.lower() in ("all day", "tentative", ""):
            return None
        try:
            return datetime.strptime(f"{d} {t}", "%m-%d-%Y %I:%M%p").replace(tzinfo=EST)
        except ValueError:
            try:
                return datetime.strptime(f"{d} {t}", "%m-%d-%Y %H:%M").replace(tzinfo=EST)
            except ValueError:
                return None

    # ── Filtering ────────────────────────────────────────────────────────
    def filter_usd_high(self, events: list[dict]) -> list[dict]:
        """USD + high-impact only."""
        out = []
        for e in events:
            if (e.get("country") or "").strip() != "USD":
                continue
            if (e.get("impact") or "").strip() != "High":
                continue
            out.append(e)
        return out

    def events_for_day(self, when: datetime) -> list[dict]:
        day_start = when.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        out = []
        for e in self.filter_usd_high(self.fetch_events()):
            t = self.parse_event_time(e)
            if t and day_start <= t < day_end:
                out.append(e)
        return sorted(out, key=lambda e: self.parse_event_time(e))

    def events_for_week(self, when: datetime) -> list[dict]:
        week_start = when - timedelta(days=when.weekday())           # Mon 00:00
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end   = week_start + timedelta(days=7)
        out = []
        for e in self.filter_usd_high(self.fetch_events()):
            t = self.parse_event_time(e)
            if t and week_start <= t < week_end:
                out.append(e)
        return sorted(out, key=lambda e: self.parse_event_time(e))

    def event_id(self, evt: dict) -> str:
        return f"{evt.get('date','')}|{evt.get('time','')}|{evt.get('title','')}"

    # ── Embeds ───────────────────────────────────────────────────────────
    def _post(self, embed: dict) -> bool:
        if not self.discord_url:
            logger.warning("NOVA_NEWS_DISCORD_WEBHOOK_URL not set — skipping post")
            return False
        if not embed:
            return False
        try:
            r = requests.post(self.discord_url, json={"embeds": [embed]}, timeout=6)
            r.raise_for_status()
            return True
        except Exception as ex:
            logger.error(f"discord post failed: {ex}")
            return False

    def fmt_daily(self, events: list[dict], day: datetime) -> dict:
        day_str = day.strftime("%A · %B %d")
        if not events:
            return {
                "title":       f"📅 Today's USD High-Impact — {day_str}",
                "description": "No high-impact USD events today. Clean tape — trade structure.",
                "color":       0x808080,
                "footer":      {"text": "NOVA News · Forex Factory"},
            }
        fields = []
        for e in events:
            t = self.parse_event_time(e)
            time_str = t.strftime("%-I:%M %p EST") if t else e.get("time", "")
            # Windows strftime doesn't support %-I; fallback
            if t and ("%-" in time_str or time_str.startswith("0")):
                time_str = t.strftime("%I:%M %p EST").lstrip("0")
            forecast = e.get("forecast") or "—"
            previous = e.get("previous") or "—"
            fields.append({
                "name":  f"🔴 {e.get('title','?')}",
                "value": f"**{time_str}** · Forecast `{forecast}` · Previous `{previous}`",
                "inline": False,
            })
        return {
            "title":       f"📅 Today's USD High-Impact — {day_str}",
            "description": f"**{len(events)} event(s)** today. Respect release windows; no entries inside ±5 min of these.",
            "color":       0xFF6B00,
            "fields":      fields[:25],
            "footer":      {"text": "NOVA News · Forex Factory"},
        }

    def fmt_weekly(self, events: list[dict], week_anchor: datetime) -> dict | None:
        week_start = week_anchor - timedelta(days=week_anchor.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        header = f"{week_start.strftime('%b %d')} – {(week_start + timedelta(days=6)).strftime('%b %d, %Y')}"
        by_day: dict[str, list[tuple[datetime, dict]]] = {}
        for e in events:
            t = self.parse_event_time(e)
            if not t:
                continue
            key = t.strftime("%a %b %d")
            by_day.setdefault(key, []).append((t, e))
        if not by_day:
            return {
                "title":       f"📆 Week Ahead — USD High-Impact · {header}",
                "description": "No high-impact USD events scheduled. Quiet week.",
                "color":       0x808080,
                "footer":      {"text": "NOVA News · Forex Factory"},
            }
        fields = []
        # Sort days chronologically
        day_keys = sorted(by_day.keys(), key=lambda k: by_day[k][0][0])
        for day in day_keys:
            items = sorted(by_day[day], key=lambda x: x[0])
            lines = []
            for t, e in items:
                time_str = t.strftime("%I:%M%p").lstrip("0").lower()
                lines.append(
                    f"`{time_str:>7}` · **{e.get('title','?')}** · f`{e.get('forecast') or '—'}` p`{e.get('previous') or '—'}`"
                )
            body = "\n".join(lines)
            if len(body) > 1024:
                body = body[:1020] + "…"
            fields.append({"name": day, "value": body, "inline": False})
        return {
            "title":       f"📆 Week Ahead — USD High-Impact · {header}",
            "description": f"**{len(events)} high-impact events** on deck this week. Plan accordingly.",
            "color":       0x00E5FF,
            "fields":      fields[:25],
            "footer":      {"text": "NOVA News · Forex Factory"},
        }

    def fmt_pre_alert(self, evt: dict) -> dict:
        t = self.parse_event_time(evt)
        time_str = t.strftime("%I:%M %p EST").lstrip("0") if t else evt.get("time", "")
        return {
            "title":       f"🔔 {evt.get('title','?')} in 10 minutes",
            "description": f"**Drops at {time_str}** — flatten positions or sit out the release.",
            "color":       0xFFB020,
            "fields": [
                {"name": "Forecast", "value": evt.get("forecast") or "—", "inline": True},
                {"name": "Previous", "value": evt.get("previous") or "—", "inline": True},
            ],
            "footer": {"text": "NOVA News · stay out of positions"},
        }

    def fmt_post_result(self, evt: dict) -> dict:
        actual   = evt.get("actual")   or "—"
        forecast = evt.get("forecast") or "—"
        previous = evt.get("previous") or "—"
        bias     = self._analyze_bias(evt.get("title", ""), actual, forecast)
        color    = {"hawkish": 0xE53E3E, "dovish": 0x00C853, "neutral": 0x808080}[bias]
        return {
            "title": f"✅ {evt.get('title','?')} — Released",
            "color": color,
            "fields": [
                {"name": "Actual",   "value": actual,   "inline": True},
                {"name": "Forecast", "value": forecast, "inline": True},
                {"name": "Previous", "value": previous, "inline": True},
                {"name": "Read",     "value": bias.upper(), "inline": False},
            ],
            "footer": {"text": "NOVA News · auto-bias heuristic — confirm with price"},
        }

    def _analyze_bias(self, title: str, actual: str, forecast: str) -> str:
        """Rough hawkish/dovish read. Returns 'hawkish'|'dovish'|'neutral'."""
        strip = lambda s: re.sub(r"[^\d\.\-]", "", s or "")
        try:
            a = float(strip(actual))
            f = float(strip(forecast))
        except (ValueError, TypeError):
            return "neutral"
        hotter   = a > f
        t_lower  = title.lower()
        # Unemployment: higher actual = more unemployment = dovish
        # Initial claims: higher actual = more unemployment = dovish
        if "unemployment" in t_lower or "claims" in t_lower:
            return "dovish" if hotter else "hawkish"
        # Default for CPI/PPI/jobs/GDP/ISM/retail: higher = stronger = hawkish
        return "hawkish" if hotter else "dovish"

    # ── Scheduled triggers ───────────────────────────────────────────────
    def maybe_post_weekly(self, force: bool = False):
        """Fire Sunday 18:00 EST + kickstart on first boot this week."""
        now = datetime.now(tz=EST)
        if not force and self._last_weekly_at and \
           (now - self._last_weekly_at) < timedelta(hours=20):
            return
        events = self.events_for_week(now)
        embed  = self.fmt_weekly(events, now)
        if embed and self._post(embed):
            self._last_weekly_at = now
            logger.info(f"posted weekly preview ({len(events)} events)")

    def maybe_post_daily(self, force: bool = False):
        """Fire Mon-Fri 07:00 EST."""
        now = datetime.now(tz=EST)
        if not force and self._last_daily_at and \
           self._last_daily_at.date() == now.date():
            return
        if not force and now.weekday() >= 5:
            return
        events = self.events_for_day(now)
        embed  = self.fmt_daily(events, now)
        if self._post(embed):
            self._last_daily_at = now
            logger.info(f"posted daily menu ({len(events)} events)")

    def scan_pre_and_post(self):
        """Check each US high-impact event: fire pre-alert 10 min out,
        fire post-result when actual appears."""
        now    = datetime.now(tz=EST)
        events = self.filter_usd_high(self.fetch_events(force=True))
        for e in events:
            t = self.parse_event_time(e)
            if not t:
                continue
            eid     = self.event_id(e)
            delta_m = (t - now).total_seconds() / 60.0
            # Pre-alert: 10 min +/- 3 min window, fire once
            if 7 <= delta_m <= 13 and eid not in self._fired_pre:
                if self._post(self.fmt_pre_alert(e)):
                    self._fired_pre.add(eid)
                    logger.info(f"pre-alert: {e.get('title')}")
            # Post-result: event passed AND actual present AND not yet fired
            actual = (e.get("actual") or "").strip()
            if delta_m < 0 and actual and actual != "—" and eid not in self._fired_post:
                if self._post(self.fmt_post_result(e)):
                    self._fired_post.add(eid)
                    logger.info(f"post-result: {e.get('title')} = {actual}")

    # ── Daemon loop ──────────────────────────────────────────────────────
    def _loop(self):
        # Kickstart: fire one weekly preview on first boot so the community
        # gets Sunday-night value even if we deploy mid-week.
        try:
            self.maybe_post_weekly(force=True)
        except Exception as e:
            logger.error(f"kickstart weekly failed: {e}")

        while not self._stop.is_set():
            try:
                now = datetime.now(tz=EST)

                # Sunday 18:00 EST → weekly preview
                if now.weekday() == 6 and now.hour == 18 and now.minute < 5:
                    self.maybe_post_weekly()

                # Mon-Fri 07:00 EST → daily menu
                if now.weekday() < 5 and now.hour == 7 and now.minute < 5:
                    self.maybe_post_daily()

                # Every tick (5 min) during Mon-Fri 06-17 EST → scan
                if now.weekday() < 5 and 6 <= now.hour <= 17:
                    self.scan_pre_and_post()

                # Daily rollover housekeeping: clear dedup maps on new day
                if now.hour == 0 and now.minute < 5:
                    self._fired_pre.clear()
                    self._fired_post.clear()

            except Exception as e:
                logger.exception(f"news agent tick error: {e}")

            # Sleep 60s — schedule granularity
            self._stop.wait(60)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="NewsAgent", daemon=True)
        self._thread.start()
        logger.info("NewsAgent daemon thread started")

    def stop(self):
        self._stop.set()


# Module-level singleton accessor for the Flask app
_agent: NewsAgent | None = None

def get_agent() -> NewsAgent:
    global _agent
    if _agent is None:
        _agent = NewsAgent()
    return _agent
