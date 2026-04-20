"""
═══════════════════════════════════════════════════════════════════════════
NOVA Headlines Agent — Live financial headlines to Discord
───────────────────────────────────────────────────────────────────────────

Polls free RSS feeds every 5 minutes during market hours (15 min off-hours)
and posts fresh headlines as Discord embeds to #live-headlines.

Sources (all free, no API key):
  - Yahoo Finance   (mainstream breaking news)
  - CNBC Top News   (fast market coverage)
  - MarketWatch     (equity focus)

Dedup:
  - On first boot, mark all currently-in-feed items as "seen" so we don't
    flood the channel on startup. Only fresh items from subsequent polls
    get posted.
  - Dedup key is the article GUID (falls back to URL).
  - Keep last 500 GUIDs per source in memory to survive daily cycle.

Breaking news coloring:
  - If title contains BREAKING / ALERT / FLASH / URGENT, use red (0xE53E3E)
  - Otherwise source-specific colors

Env:
  NOVA_HEADLINES_DISCORD_WEBHOOK_URL  — Discord webhook for the channel
  NOVA_HEADLINES_POLL_INTERVAL_LIVE   — seconds between polls during market hours (default 300)
  NOVA_HEADLINES_POLL_INTERVAL_QUIET  — seconds between polls off-hours (default 900)
  NOVA_HEADLINES_MAX_PER_CYCLE        — cap posts per poll cycle per source (default 3)
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests


EST = ZoneInfo("America/New_York")
logger = logging.getLogger("nova-headlines-agent")


# ── Source definitions ──────────────────────────────────────────────────────
@dataclass
class Source:
    name:  str
    url:   str
    color: int     # Discord embed side-bar color


SOURCES: list[Source] = [
    Source("Yahoo Finance", "https://finance.yahoo.com/news/rssindex",                  0x6001D2),
    Source("CNBC",          "https://www.cnbc.com/id/100003114/device/rss/rss.html",    0xCC0000),
    Source("MarketWatch",   "https://feeds.content.dowjones.io/public/rss/mw_topstories", 0x008744),
]

BREAKING_RE = re.compile(r"\b(BREAKING|ALERT|FLASH|URGENT|JUST IN)\b", re.IGNORECASE)
BREAKING_COLOR = 0xE53E3E
MAX_DEDUP_MEMORY = 500


class HeadlinesAgent:
    """RSS → Discord relay for the #live-headlines channel."""

    def __init__(self, discord_url: str | None = None):
        self.discord_url        = discord_url or os.environ.get("NOVA_HEADLINES_DISCORD_WEBHOOK_URL", "")
        self.poll_live          = int(os.environ.get("NOVA_HEADLINES_POLL_INTERVAL_LIVE",  "300"))
        self.poll_quiet         = int(os.environ.get("NOVA_HEADLINES_POLL_INTERVAL_QUIET", "900"))
        self.max_per_cycle      = int(os.environ.get("NOVA_HEADLINES_MAX_PER_CYCLE",        "3"))
        self._stop              = threading.Event()
        self._thread            = None
        self._seen_per_source:  dict[str, deque[str]] = {s.name: deque(maxlen=MAX_DEDUP_MEMORY) for s in SOURCES}
        self._first_cycle       = True

    # ── Fetch ────────────────────────────────────────────────────────────
    def _fetch_rss(self, url: str) -> list[dict]:
        """Pull a feed and return a list of normalized item dicts."""
        try:
            r = requests.get(url, timeout=8, headers={
                "User-Agent": "NOVA Headlines Agent / 1.0",
                "Accept":     "application/rss+xml, application/xml, text/xml, */*",
            })
            r.raise_for_status()
        except Exception as ex:
            logger.warning(f"fetch failed {url}: {ex}")
            return []
        items: list[dict] = []
        try:
            # Strip BOM / whitespace that breaks ElementTree sometimes
            content = r.content.lstrip()
            root = ET.fromstring(content)
            # RSS 2.0: rss > channel > item
            ns_free = lambda tag: tag.split("}", 1)[-1]  # drop namespace prefix
            for item in root.iter():
                if ns_free(item.tag) != "item":
                    continue
                obj = {}
                for child in item:
                    k = ns_free(child.tag)
                    t = (child.text or "").strip()
                    if k not in obj and t:
                        obj[k] = t
                if "title" in obj and ("link" in obj or "guid" in obj):
                    items.append(obj)
        except ET.ParseError as ex:
            logger.warning(f"parse failed {url}: {ex}")
        return items[:30]  # cap per-fetch so absurd feeds don't blow memory

    # ── Dedup ────────────────────────────────────────────────────────────
    def _item_id(self, item: dict) -> str:
        return item.get("guid") or item.get("link") or item.get("title", "")

    def _new_items(self, source: Source, items: list[dict]) -> list[dict]:
        seen = self._seen_per_source[source.name]
        fresh = []
        for item in items:
            iid = self._item_id(item)
            if iid in seen:
                continue
            fresh.append(item)
            seen.append(iid)
        return fresh

    # ── Embed ────────────────────────────────────────────────────────────
    def _strip_html(self, s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s or "")
        s = html.unescape(s)
        return re.sub(r"\s+", " ", s).strip()

    def _parse_pubdate(self, item: dict) -> datetime | None:
        for key in ("pubDate", "published", "updated", "date"):
            raw = item.get(key)
            if not raw:
                continue
            for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                        "%a, %d %b %Y %H:%M:%S %Z",
                        "%Y-%m-%dT%H:%M:%S%z",
                        "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(EST)
                except ValueError:
                    continue
        return None

    def _build_embed(self, source: Source, item: dict) -> dict:
        title = self._strip_html(item.get("title", ""))[:256]
        link  = item.get("link") or ""
        desc  = self._strip_html(item.get("description", "") or item.get("summary", ""))[:400]
        pub   = self._parse_pubdate(item)
        breaking = bool(BREAKING_RE.search(title))
        color = BREAKING_COLOR if breaking else source.color
        embed = {
            "title":       title,
            "url":         link,
            "color":       color,
            "description": desc if desc else None,
            "footer": {"text": f"{source.name}" + (f" · {pub.strftime('%I:%M %p EST').lstrip('0')}" if pub else "")},
        }
        # Drop None description so Discord doesn't complain
        if not embed["description"]:
            embed.pop("description")
        return embed

    # ── Post ─────────────────────────────────────────────────────────────
    def _post(self, embed: dict) -> bool:
        if not self.discord_url:
            logger.warning("NOVA_HEADLINES_DISCORD_WEBHOOK_URL not set")
            return False
        try:
            r = requests.post(self.discord_url, json={"embeds": [embed]}, timeout=6)
            if r.status_code == 204:
                return True
            if r.status_code == 429:
                # Rate-limited — Discord says sleep N ms
                retry = r.json().get("retry_after", 1)
                logger.warning(f"rate limited, sleep {retry}s")
                time.sleep(float(retry))
                return False
            r.raise_for_status()
            return True
        except Exception as ex:
            logger.error(f"discord post failed: {ex}")
            return False

    # ── Cycle ────────────────────────────────────────────────────────────
    def tick(self, force_post: bool = False):
        """One polling cycle. Returns total posts made."""
        posted = 0
        for src in SOURCES:
            items = self._fetch_rss(src.url)
            fresh = self._new_items(src, items)
            if self._first_cycle and not force_post:
                # Warm the dedup without posting
                continue
            # Post freshest first, capped
            for item in fresh[:self.max_per_cycle]:
                embed = self._build_embed(src, item)
                if self._post(embed):
                    posted += 1
                    time.sleep(0.5)  # Discord rate-limit politeness
        self._first_cycle = False
        return posted

    # ── Daemon ───────────────────────────────────────────────────────────
    def _in_live_hours(self) -> bool:
        now = datetime.now(tz=EST)
        if now.weekday() >= 5:
            return False
        mins = now.hour * 60 + now.minute
        return 6*60 <= mins < 17*60   # 06:00-17:00 EST

    def _loop(self):
        time.sleep(5)  # let server finish booting
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.exception(f"tick error: {e}")
            interval = self.poll_live if self._in_live_hours() else self.poll_quiet
            self._stop.wait(interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="HeadlinesAgent", daemon=True)
        self._thread.start()
        logger.info(f"HeadlinesAgent started — {len(SOURCES)} sources, {self.poll_live}s live / {self.poll_quiet}s quiet")

    def stop(self):
        self._stop.set()


# Module-level singleton
_agent: HeadlinesAgent | None = None

def get_agent() -> HeadlinesAgent:
    global _agent
    if _agent is None:
        _agent = HeadlinesAgent()
    return _agent
