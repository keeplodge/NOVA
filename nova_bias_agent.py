"""
═══════════════════════════════════════════════════════════════════════════
NOVA Daily Bias Agent — #daily-bias Discord channel
───────────────────────────────────────────────────────────────────────────

Pulls NQ / VIX / DXY / 10Y yield via yfinance, computes an auto bias score
(LONG / SHORT / NEUTRAL with weak/moderate/strong confidence), and posts
one embed per day at 07:45 EST Mon-Fri.

Includes today's high-impact USD events (from the news agent's FF feed)
as a "news risk" section so the community knows when to sit out.

Env:
  NOVA_BIAS_DISCORD_WEBHOOK_URL
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


EST = ZoneInfo("America/New_York")
logger = logging.getLogger("nova-bias-agent")


def _get_yf():
    import yfinance as yf
    return yf


class BiasAgent:
    """Daily NQ directional bias for the community."""

    def __init__(self, discord_url: str | None = None):
        self.discord_url   = discord_url or os.environ.get("NOVA_BIAS_DISCORD_WEBHOOK_URL", "")
        self._stop         = threading.Event()
        self._thread       = None
        self._last_post_date = None

    # ── Market data fetch ───────────────────────────────────────────────
    def _safe_float(self, v) -> float | None:
        """Flatten pandas Series/scalar to float, None on failure."""
        try:
            if hasattr(v, "iloc"):
                v = v.iloc[0]
            return float(v)
        except Exception:
            return None

    def _fetch_levels(self) -> dict:
        """Pulls NQ daily + intraday, VIX, DXY, 10Y. Uses yf.Ticker (per-symbol)
        which is more Railway-network-friendly than yf.download (bulk)."""
        yf = _get_yf()
        out: dict = {}
        errors: list[str] = []

        # 1. NQ daily for PDH / PDL / prev_close
        try:
            nq = yf.Ticker("NQ=F").history(period="5d", interval="1d", auto_adjust=False)
            if len(nq) >= 2:
                prev = nq.iloc[-2]
                out["pdh"]        = self._safe_float(prev["High"])
                out["pdl"]        = self._safe_float(prev["Low"])
                out["prev_close"] = self._safe_float(prev["Close"])
                if all(out.get(k) for k in ("pdh", "pdl", "prev_close")):
                    out["prior_typ"] = (out["pdh"] + out["pdl"] + out["prev_close"]) / 3
        except Exception as ex:
            errors.append(f"NQ daily: {ex}")

        # 2. NQ intraday for current price + Asian range
        try:
            nq_i = yf.Ticker("NQ=F").history(period="2d", interval="15m", auto_adjust=False)
            if len(nq_i) > 0:
                out["nq_price"] = self._safe_float(nq_i["Close"].iloc[-1])
                today   = datetime.now(tz=EST).replace(hour=0, minute=0, second=0, microsecond=0)
                asia_s  = today - timedelta(hours=5)
                asia_e  = today
                idx     = nq_i.index
                try:
                    # Index may be tz-aware or naive; make the comparison work either way
                    if hasattr(idx, "tz") and idx.tz is not None:
                        asia_s_cmp = asia_s.astimezone(idx.tz)
                        asia_e_cmp = asia_e.astimezone(idx.tz)
                    else:
                        asia_s_cmp = asia_s.replace(tzinfo=None)
                        asia_e_cmp = asia_e.replace(tzinfo=None)
                    mask = (idx >= asia_s_cmp) & (idx < asia_e_cmp)
                    if mask.any():
                        asi = nq_i.loc[mask]
                        out["asian_high"] = self._safe_float(asi["High"].max())
                        out["asian_low"]  = self._safe_float(asi["Low"].min())
                except Exception as ex:
                    errors.append(f"asian range: {ex}")
        except Exception as ex:
            errors.append(f"NQ intraday: {ex}")

        # 3. Macro: VIX / DXY / 10Y
        for sym, key in [("^VIX", "vix"), ("DX-Y.NYB", "dxy"), ("^TNX", "tnx")]:
            try:
                d = yf.Ticker(sym).history(period="3d", interval="1d", auto_adjust=False)
                if len(d) >= 2:
                    cur  = self._safe_float(d["Close"].iloc[-1])
                    prev = self._safe_float(d["Close"].iloc[-2])
                    if cur is not None:
                        out[key] = cur
                    if cur is not None and prev:
                        out[f"{key}_pct"] = (cur - prev) / prev * 100
            except Exception as ex:
                errors.append(f"{sym}: {ex}")

        if errors:
            logger.warning(f"bias fetch errors: {errors[:3]}")
        out["_errors"] = errors  # expose for /bias/fire diagnostic
        return out

    # ── Bias scoring ────────────────────────────────────────────────────
    def compute_bias(self, ctx: dict) -> dict:
        bull = 0
        bear = 0
        reasons_bull = []
        reasons_bear = []

        # 1. NQ vs prior close
        if "nq_price" in ctx and "prev_close" in ctx:
            if ctx["nq_price"] > ctx["prev_close"]:
                bull += 1; reasons_bull.append("NQ > prior close")
            elif ctx["nq_price"] < ctx["prev_close"]:
                bear += 1; reasons_bear.append("NQ < prior close")

        # 2. VIX level
        if "vix" in ctx:
            if ctx["vix"] < 15:
                bull += 1; reasons_bull.append(f"VIX calm ({ctx['vix']:.1f})")
            elif ctx["vix"] > 20:
                bear += 1; reasons_bear.append(f"VIX elevated ({ctx['vix']:.1f})")

        # 3. DXY day change
        if "dxy_pct" in ctx:
            if ctx["dxy_pct"] < -0.2:
                bull += 1; reasons_bull.append(f"DXY soft ({ctx['dxy_pct']:+.2f}%)")
            elif ctx["dxy_pct"] > 0.2:
                bear += 1; reasons_bear.append(f"DXY firm ({ctx['dxy_pct']:+.2f}%)")

        # 4. NQ vs Asian range (2 pts — strong structural signal)
        if "nq_price" in ctx and "asian_high" in ctx and "asian_low" in ctx:
            if ctx["nq_price"] > ctx["asian_high"]:
                bull += 2; reasons_bull.append("NQ broke Asian range high")
            elif ctx["nq_price"] < ctx["asian_low"]:
                bear += 2; reasons_bear.append("NQ broke Asian range low")

        diff = bull - bear
        if abs(diff) <= 1:
            label = "NEUTRAL"; strength = "range"
        else:
            label = "LONG" if diff > 0 else "SHORT"
            mag = abs(diff)
            strength = "weak" if mag == 2 else ("moderate" if mag == 3 else "strong")

        return {
            "bias":     label,
            "strength": strength,
            "bull":     bull,
            "bear":     bear,
            "reasons_bull": reasons_bull,
            "reasons_bear": reasons_bear,
        }

    # ── Today's news ────────────────────────────────────────────────────
    def _todays_news(self) -> list[dict]:
        try:
            from nova_news_agent import get_agent as news_get
            na = news_get()
            return na.events_for_day(datetime.now(tz=EST))
        except Exception as ex:
            logger.warning(f"news fetch failed: {ex}")
            return []

    # ── Format ──────────────────────────────────────────────────────────
    def _fmt_level(self, v: float | None) -> str:
        if v is None: return "—"
        return f"{v:,.0f}" if v > 100 else f"{v:,.2f}"

    def fmt_embed(self, ctx: dict, bias: dict) -> dict:
        now = datetime.now(tz=EST)
        day_str = now.strftime("%A · %b %d, %Y")

        bias_color = {
            "LONG":    0x00C853,
            "SHORT":   0xE53E3E,
            "NEUTRAL": 0xFFB020,
        }[bias["bias"]]

        bias_emoji = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "⚖️"}[bias["bias"]]

        # Build reasons line
        if bias["bias"] == "NEUTRAL":
            reasons = "Conflicting signals — no clear edge. Trade the range."
        else:
            picks = bias["reasons_bull"] if bias["bias"] == "LONG" else bias["reasons_bear"]
            reasons = " · ".join(picks[:3]) or "—"

        # Key levels
        levels_lines = []
        if "pdh" in ctx:        levels_lines.append(f"`PDH       ` {self._fmt_level(ctx['pdh'])}")
        if "pdl" in ctx:        levels_lines.append(f"`PDL       ` {self._fmt_level(ctx['pdl'])}")
        if "asian_high" in ctx: levels_lines.append(f"`Asian High` {self._fmt_level(ctx['asian_high'])}")
        if "asian_low" in ctx:  levels_lines.append(f"`Asian Low ` {self._fmt_level(ctx['asian_low'])}")
        if "prior_typ" in ctx:  levels_lines.append(f"`Prior VWAP` {self._fmt_level(ctx['prior_typ'])}")
        if "nq_price" in ctx:   levels_lines.append(f"`NQ Now    ` {self._fmt_level(ctx['nq_price'])}")

        # Macro
        macro_lines = []
        if "vix" in ctx:
            tag = " (risk-on)" if ctx["vix"] < 15 else (" (elevated)" if ctx["vix"] > 20 else "")
            macro_lines.append(f"`VIX` {ctx['vix']:.1f}{tag}")
        if "dxy" in ctx and "dxy_pct" in ctx:
            macro_lines.append(f"`DXY` {ctx['dxy']:.2f} ({ctx['dxy_pct']:+.2f}%)")
        if "tnx" in ctx:
            # TNX is 10Y yield × 10 (so 42.5 = 4.25%)
            yield_pct = ctx["tnx"] / 10.0 if ctx["tnx"] > 20 else ctx["tnx"]
            macro_lines.append(f"`10Y` {yield_pct:.2f}%")

        # News risk
        news_events = self._todays_news()
        if news_events:
            news_lines = []
            for e in news_events[:5]:
                t = e.get("time", "")
                title = e.get("title", "")
                fcst  = e.get("forecast") or "—"
                news_lines.append(f"`{t:>7}` **{title}** · f `{fcst}`")
            news_text = "\n".join(news_lines)
        else:
            news_text = "Clean day — no high-impact USD events."

        # Playbook
        if bias["bias"] == "LONG":
            playbook = (
                f"Sweep **PDL** ({self._fmt_level(ctx.get('pdl'))}) → MSS → FVG → **LONG**\n"
                f"Invalidation below {self._fmt_level(ctx.get('pdl',0) - 5 if ctx.get('pdl') else None)}\n"
                f"Target: **{self._fmt_level(ctx.get('pdh'))}** (PDH)"
            )
        elif bias["bias"] == "SHORT":
            playbook = (
                f"Sweep **PDH** ({self._fmt_level(ctx.get('pdh'))}) → MSS → FVG → **SHORT**\n"
                f"Invalidation above {self._fmt_level(ctx.get('pdh',0) + 5 if ctx.get('pdh') else None)}\n"
                f"Target: **{self._fmt_level(ctx.get('pdl'))}** (PDL)"
            )
        else:
            playbook = (
                f"Range trade: **PDL** {self._fmt_level(ctx.get('pdl'))} ↔ **PDH** {self._fmt_level(ctx.get('pdh'))}\n"
                f"Scalp only — wait for a clean sweep + MSS at either extreme.\n"
                f"Skip if: NQ breaks and holds outside range without a sweep."
            )

        return {
            "title":       f"📊 NOVA Daily Bias — {day_str}",
            "color":       bias_color,
            "description": f"**{bias_emoji} BIAS: {bias['bias']} ({bias['strength']})**\n{reasons}",
            "fields": [
                {"name": "🎯 Key Levels",   "value": "\n".join(levels_lines) or "—",             "inline": False},
                {"name": "⚖️ Macro",       "value": "\n".join(macro_lines) or "—",              "inline": False},
                {"name": "📰 News Risk",    "value": news_text[:1024],                           "inline": False},
                {"name": "💡 Playbook",     "value": playbook,                                    "inline": False},
            ],
            "footer": {"text": f"NOVA Bias · bull:{bias['bull']} bear:{bias['bear']} · {now.strftime('%H:%M EST')}"},
        }

    # ── Post ────────────────────────────────────────────────────────────
    def _post(self, embed: dict) -> bool:
        if not self.discord_url:
            logger.warning("NOVA_BIAS_DISCORD_WEBHOOK_URL not set")
            return False
        try:
            r = requests.post(self.discord_url, json={"embeds": [embed]}, timeout=6)
            return r.status_code in (200, 204)
        except Exception as ex:
            logger.error(f"discord post failed: {ex}")
            return False

    def maybe_post(self, force: bool = False):
        now = datetime.now(tz=EST)
        if not force and self._last_post_date == now.date():
            return
        if not force and now.weekday() >= 5:
            return
        ctx = self._fetch_levels()
        if not ctx:
            logger.warning("bias: no context — skipping post")
            return
        bias  = self.compute_bias(ctx)
        embed = self.fmt_embed(ctx, bias)
        if self._post(embed):
            self._last_post_date = now.date()

    # ── Loop ────────────────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.is_set():
            try:
                now = datetime.now(tz=EST)
                # Mon-Fri 07:45 EST only
                if now.weekday() < 5 and now.hour == 7 and 45 <= now.minute < 50:
                    self.maybe_post()
            except Exception as e:
                logger.exception(f"bias tick error: {e}")
            self._stop.wait(60)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="BiasAgent", daemon=True)
        self._thread.start()
        logger.info("BiasAgent started")

    def stop(self):
        self._stop.set()


# Module-level singleton
_agent: BiasAgent | None = None

def get_agent() -> BiasAgent:
    global _agent
    if _agent is None:
        _agent = BiasAgent()
    return _agent
