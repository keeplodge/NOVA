"""
═══════════════════════════════════════════════════════════════════════════
NOVA Watchlist Agent — Hunnid Ticks #watchlist channel
───────────────────────────────────────────────────────────────────────────

Bulk-polls yfinance for a curated basket of NQ-100 leaders, crypto
(BTC/ETH/XRP), and gold. Posts Discord embeds on a tiered schedule:

  07:30 EST Mon-Fri    Morning watchlist (full table)
  10:00-16:00 EST Mon-Fri hourly  Intraday movers
  16:05 EST Mon-Fri    EOD winners/losers
  Big-move alerts      Any ticker moves >3% (stocks) or >2% (crypto/gold)
                       in the last hour → red/green solo embed
  09:00 / 17:00 EST Sat-Sun  Crypto-only weekend snapshot

Env:
  NOVA_WATCHLIST_DISCORD_WEBHOOK_URL
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


EST = ZoneInfo("America/New_York")
logger = logging.getLogger("nova-watchlist-agent")


# ── Ticker basket ───────────────────────────────────────────────────────────
@dataclass
class Ticker:
    symbol:   str    # yfinance symbol
    display:  str    # what to show in embeds
    category: str    # "stock" | "crypto" | "commodity" | "index"
    big_move_pct: float  # threshold for big-move alerts (%)

TICKERS: list[Ticker] = [
    # NQ leaders
    Ticker("AAPL",    "AAPL",    "stock",     3.0),
    Ticker("MSFT",    "MSFT",    "stock",     3.0),
    Ticker("NVDA",    "NVDA",    "stock",     3.0),
    Ticker("AMZN",    "AMZN",    "stock",     3.0),
    Ticker("META",    "META",    "stock",     3.0),
    Ticker("GOOGL",   "GOOGL",   "stock",     3.0),
    Ticker("TSLA",    "TSLA",    "stock",     4.0),   # Tesla is noisier, higher bar
    Ticker("AMD",     "AMD",     "stock",     3.0),
    Ticker("AVGO",    "AVGO",    "stock",     3.0),
    Ticker("NFLX",    "NFLX",    "stock",     3.0),
    # Crypto (24/7)
    Ticker("BTC-USD", "BTC",     "crypto",    2.0),
    Ticker("ETH-USD", "ETH",     "crypto",    2.0),
    Ticker("XRP-USD", "XRP",     "crypto",    2.5),   # more volatile
    # Commodity
    Ticker("GC=F",    "XAUUSD",  "commodity", 2.0),
    # Index reference
    Ticker("^NDX",    "NDX",     "index",     2.0),
]


def _get_yf():
    import yfinance as yf  # lazy import so parse-only tests don't pull the lib
    return yf


# ── Agent ───────────────────────────────────────────────────────────────────

class WatchlistAgent:

    def __init__(self, discord_url: str | None = None):
        self.discord_url = discord_url or os.environ.get("NOVA_WATCHLIST_DISCORD_WEBHOOK_URL", "")
        self._stop = threading.Event()
        self._thread = None
        self._hour_ago_prices: dict[str, float] = {}   # sym → price ~1h ago
        self._last_big_move_at: dict[str, datetime] = {}  # sym → when we last alerted
        self._last_morning_date = None
        self._last_intraday_hour = None
        self._last_eod_date = None
        self._last_weekend_hour = None

    # ── Fetch ────────────────────────────────────────────────────────────
    def _fetch_quotes(self) -> dict[str, dict]:
        """
        Returns {symbol: {price, prev_close, pct_change, day_high, day_low}}
        for all tickers. Uses yf.download bulk to minimize HTTP calls.
        """
        yf = _get_yf()
        symbols = [t.symbol for t in TICKERS]
        try:
            data = yf.download(
                tickers=" ".join(symbols),
                period="5d",
                interval="1d",
                group_by="ticker",
                progress=False,
                threads=True,
                auto_adjust=False,
            )
        except Exception as ex:
            logger.error(f"bulk quote fetch failed: {ex}")
            return {}
        out: dict[str, dict] = {}
        for sym in symbols:
            try:
                df = data[sym] if len(symbols) > 1 else data
                closes = df["Close"].dropna()
                if len(closes) < 2:
                    continue
                price      = float(closes.iloc[-1])
                prev_close = float(closes.iloc[-2])
                pct        = (price - prev_close) / prev_close * 100 if prev_close else 0.0
                day_high   = float(df["High"].iloc[-1])
                day_low    = float(df["Low"].iloc[-1])
                out[sym] = {
                    "price":       price,
                    "prev_close":  prev_close,
                    "pct_change":  pct,
                    "day_high":    day_high,
                    "day_low":     day_low,
                }
            except Exception:
                continue
        return out

    def _fetch_intraday_hour(self) -> dict[str, float]:
        """Pull 1h-ago prices (using 15m bars) to detect hourly moves."""
        yf = _get_yf()
        symbols = [t.symbol for t in TICKERS]
        try:
            data = yf.download(
                tickers=" ".join(symbols),
                period="1d",
                interval="15m",
                group_by="ticker",
                progress=False,
                threads=True,
                auto_adjust=False,
            )
        except Exception as ex:
            logger.warning(f"intraday fetch failed: {ex}")
            return {}
        out = {}
        for sym in symbols:
            try:
                df = data[sym] if len(symbols) > 1 else data
                closes = df["Close"].dropna()
                if len(closes) < 5:
                    continue
                # 4 bars back = 1h at 15m interval
                out[sym] = float(closes.iloc[-5])
            except Exception:
                continue
        return out

    # ── Formatters ───────────────────────────────────────────────────────
    def _arrow(self, pct: float) -> str:
        return "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")

    def _fmt_price(self, p: float, t: Ticker) -> str:
        if t.category == "crypto" and p >= 1000: return f"${p:,.0f}"
        if t.category == "crypto":               return f"${p:,.4f}".rstrip("0").rstrip(".")
        if p >= 1000:                            return f"${p:,.0f}"
        if p >= 10:                              return f"${p:,.2f}"
        return f"${p:.4f}"

    def _fmt_line(self, t: Ticker, q: dict) -> str:
        price = self._fmt_price(q["price"], t)
        pct   = q["pct_change"]
        arrow = self._arrow(pct)
        sign  = "+" if pct >= 0 else ""
        return f"`{t.display:<6}` {price:<11}  {sign}{pct:.2f}%  {arrow}"

    def _group_by_cat(self, quotes: dict[str, dict]) -> dict[str, list[tuple[Ticker, dict]]]:
        groups: dict[str, list] = {}
        for t in TICKERS:
            if t.symbol in quotes:
                groups.setdefault(t.category, []).append((t, quotes[t.symbol]))
        return groups

    def fmt_morning(self, quotes: dict[str, dict]) -> dict:
        now = datetime.now(tz=EST)
        groups = self._group_by_cat(quotes)
        fields = []
        LABELS = {
            "stock":     "🏢 NQ Leaders",
            "crypto":    "💰 Crypto",
            "commodity": "🥇 Gold",
            "index":     "📊 Index",
        }
        for cat in ("stock", "crypto", "commodity", "index"):
            if cat not in groups:
                continue
            lines = [self._fmt_line(t, q) for t, q in groups[cat]]
            fields.append({"name": LABELS[cat], "value": "\n".join(lines)[:1024], "inline": False})
        return {
            "title":       f"🌅 Morning Watchlist — {now.strftime('%A · %b %d')}",
            "description": "Yesterday's close → today. Set your levels before the bell.",
            "color":       0xFF6B00,
            "fields":      fields,
            "footer":      {"text": "NOVA Watchlist · Yahoo Finance"},
        }

    def fmt_intraday(self, quotes: dict[str, dict]) -> dict | None:
        groups = self._group_by_cat(quotes)
        # Top 5 absolute movers across stocks, plus crypto/commodity overview
        all_stocks = groups.get("stock", [])
        movers = sorted(all_stocks, key=lambda x: abs(x[1]["pct_change"]), reverse=True)[:5]
        if not movers:
            return None
        now = datetime.now(tz=EST)
        mover_lines = [self._fmt_line(t, q) for t, q in movers]
        fields = [{"name": "📈 Top Stock Movers", "value": "\n".join(mover_lines), "inline": False}]
        # Crypto + gold compact
        for cat in ("crypto", "commodity"):
            if cat in groups:
                lines = [self._fmt_line(t, q) for t, q in groups[cat]]
                fields.append({
                    "name": "💰 Crypto" if cat == "crypto" else "🥇 Gold",
                    "value": "\n".join(lines),
                    "inline": True,
                })
        return {
            "title":   f"⏱ Intraday Snapshot — {now.strftime('%-I:%M %p EST').lstrip('0')}",
            "color":   0x00E5FF,
            "fields":  fields,
            "footer":  {"text": "NOVA Watchlist · live"},
        }

    def fmt_eod(self, quotes: dict[str, dict]) -> dict:
        groups = self._group_by_cat(quotes)
        all_items = []
        for cat in ("stock", "crypto", "commodity", "index"):
            all_items.extend(groups.get(cat, []))
        winners = sorted([i for i in all_items if i[1]["pct_change"] > 0], key=lambda x: x[1]["pct_change"], reverse=True)[:5]
        losers  = sorted([i for i in all_items if i[1]["pct_change"] < 0], key=lambda x: x[1]["pct_change"])[:5]
        now = datetime.now(tz=EST)
        fields = []
        if winners:
            fields.append({"name": "🟢 Top 5 Winners", "value": "\n".join(self._fmt_line(t, q) for t, q in winners), "inline": False})
        if losers:
            fields.append({"name": "🔴 Top 5 Losers",  "value": "\n".join(self._fmt_line(t, q) for t, q in losers),  "inline": False})
        return {
            "title":       f"🌆 End of Day — {now.strftime('%A · %b %d')}",
            "description": "Day's winners and losers across the watchlist.",
            "color":       0x00C853 if winners and (not losers or abs(winners[0][1]['pct_change']) > abs(losers[0][1]['pct_change'])) else 0xE53E3E,
            "fields":      fields,
            "footer":      {"text": "NOVA Watchlist · EOD"},
        }

    def fmt_big_move(self, t: Ticker, q: dict, hour_ago: float) -> dict:
        move_pct = (q["price"] - hour_ago) / hour_ago * 100 if hour_ago else 0
        color = 0x00C853 if move_pct > 0 else 0xE53E3E
        sign  = "+" if move_pct >= 0 else ""
        arrow = "📈" if move_pct > 0 else "📉"
        return {
            "title":       f"{arrow} {t.display} · {sign}{move_pct:.2f}% in 1h",
            "description": f"Price now: **{self._fmt_price(q['price'], t)}** · 1h ago: **{self._fmt_price(hour_ago, t)}**",
            "color":       color,
            "footer":      {"text": f"NOVA Watchlist · big move threshold {t.big_move_pct}%"},
        }

    def fmt_weekend_crypto(self, quotes: dict[str, dict]) -> dict:
        now = datetime.now(tz=EST)
        crypto = [(t, quotes[t.symbol]) for t in TICKERS if t.category == "crypto" and t.symbol in quotes]
        commodity = [(t, quotes[t.symbol]) for t in TICKERS if t.category == "commodity" and t.symbol in quotes]
        fields = []
        if crypto:
            fields.append({"name": "💰 Crypto", "value": "\n".join(self._fmt_line(t, q) for t, q in crypto), "inline": False})
        if commodity:
            fields.append({"name": "🥇 Gold",   "value": "\n".join(self._fmt_line(t, q) for t, q in commodity), "inline": False})
        return {
            "title":  f"🌙 Weekend Watch — {now.strftime('%a %b %d · %-I:%M %p EST').lstrip('0')}",
            "color":  0x6001D2,
            "fields": fields,
            "footer": {"text": "NOVA Watchlist · 24/7 markets"},
        }

    # ── Post ─────────────────────────────────────────────────────────────
    def _post(self, embed: dict | None) -> bool:
        if not embed:
            return False
        if not self.discord_url:
            logger.warning("NOVA_WATCHLIST_DISCORD_WEBHOOK_URL not set")
            return False
        try:
            r = requests.post(self.discord_url, json={"embeds": [embed]}, timeout=6)
            if r.status_code == 204:
                return True
            r.raise_for_status()
            return True
        except Exception as ex:
            logger.error(f"discord post failed: {ex}")
            return False

    # ── Scheduled triggers ───────────────────────────────────────────────
    def maybe_post_morning(self, force: bool = False):
        now = datetime.now(tz=EST)
        if not force and self._last_morning_date == now.date():
            return
        if not force and now.weekday() >= 5:
            return
        quotes = self._fetch_quotes()
        if quotes and self._post(self.fmt_morning(quotes)):
            self._last_morning_date = now.date()

    def maybe_post_intraday(self, force: bool = False):
        now = datetime.now(tz=EST)
        if not force and (self._last_intraday_hour == now.hour or now.weekday() >= 5):
            return
        quotes = self._fetch_quotes()
        if quotes and self._post(self.fmt_intraday(quotes)):
            self._last_intraday_hour = now.hour

    def maybe_post_eod(self, force: bool = False):
        now = datetime.now(tz=EST)
        if not force and (self._last_eod_date == now.date() or now.weekday() >= 5):
            return
        quotes = self._fetch_quotes()
        if quotes and self._post(self.fmt_eod(quotes)):
            self._last_eod_date = now.date()

    def maybe_post_weekend(self, force: bool = False):
        now = datetime.now(tz=EST)
        if not force and self._last_weekend_hour == now.hour:
            return
        if not force and now.weekday() < 5:
            return
        quotes = self._fetch_quotes()
        if quotes and self._post(self.fmt_weekend_crypto(quotes)):
            self._last_weekend_hour = now.hour

    def check_big_moves(self):
        """Detect 1-hour big moves and alert. Dedup per-ticker within 2h."""
        now = datetime.now(tz=EST)
        hour_ago = self._fetch_intraday_hour()
        if not hour_ago:
            return
        quotes = self._fetch_quotes()
        for t in TICKERS:
            if t.symbol not in quotes or t.symbol not in hour_ago:
                continue
            q       = quotes[t.symbol]
            prev    = hour_ago[t.symbol]
            pct     = (q["price"] - prev) / prev * 100 if prev else 0
            if abs(pct) < t.big_move_pct:
                continue
            last = self._last_big_move_at.get(t.symbol)
            if last and (now - last) < timedelta(hours=2):
                continue
            if self._post(self.fmt_big_move(t, q, prev)):
                self._last_big_move_at[t.symbol] = now

    # ── Loop ─────────────────────────────────────────────────────────────
    def _loop(self):
        time.sleep(10)  # give the server a head start
        while not self._stop.is_set():
            try:
                now = datetime.now(tz=EST)
                # Scheduled weekday posts
                if now.weekday() < 5:
                    if now.hour == 7  and now.minute >= 30: self.maybe_post_morning()
                    if now.hour >= 10 and now.hour < 16:    self.maybe_post_intraday()
                    if now.hour == 16 and now.minute >= 5:  self.maybe_post_eod()
                    # Big-move scan during market hours
                    if 9 <= now.hour < 16:
                        self.check_big_moves()
                else:
                    # Weekend crypto snapshot at 9am/5pm
                    if now.hour in (9, 17) and now.minute < 5:
                        self.maybe_post_weekend()
                    # Big-move scan for crypto only (filter inside check is threshold-based; safe)
                    self.check_big_moves()
            except Exception as e:
                logger.exception(f"watchlist tick error: {e}")
            # 15-min cadence — enough for big-move detection without spam
            self._stop.wait(900)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="WatchlistAgent", daemon=True)
        self._thread.start()
        logger.info(f"WatchlistAgent started — {len(TICKERS)} tickers")

    def stop(self):
        self._stop.set()


# Module-level singleton
_agent: WatchlistAgent | None = None

def get_agent() -> WatchlistAgent:
    global _agent
    if _agent is None:
        _agent = WatchlistAgent()
    return _agent
