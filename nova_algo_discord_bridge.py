"""
nova_algo_discord_bridge.py — Railway → NOVA Algo Discord webhooks.

Posts trading events into the NOVA Algo Discord server channels via
per-channel webhooks. Read-only side: Railway emits, Discord receives.

This is the *public* community side. Sir's *personal* mirror to his own
Discord (Observability in nova_trading_agents.py) is unaffected — both
coexist, different audiences, different webhooks.

Channels (env vars on Railway):
  DISCORD_LIVE_SIGNALS_WEBHOOK_URL     — every fired signal
  DISCORD_HALT_EVENTS_WEBHOOK_URL      — DD halts, gate trips, dispatch fails
  DISCORD_FANOUT_FAILURES_WEBHOOK_URL  — per-subscriber webhook delivery failures
  DISCORD_EQUITY_CURVE_WEBHOOK_URL     — daily account snapshots
  DISCORD_MORNING_BRIEF_WEBHOOK_URL    — pre-session brief
  DISCORD_EOD_RECAP_WEBHOOK_URL        — end-of-day P&L
  DISCORD_STATUS_WEBHOOK_URL           — heartbeat, deploys, incidents

Every post is env-gated: if a URL is unset, it's a silent no-op so dev
boxes / partial config never error. All HTTP calls are short-timeout and
swallow exceptions — Discord posting must NEVER fail a trade dispatch.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

EST = ZoneInfo("America/New_York")

NOVA_CYAN = 0x00F5D4
NOVA_GREEN = 0x22C55E
NOVA_RED = 0xEF4444
NOVA_AMBER = 0xF59E0B

_WEBHOOK_ENV = {
    "live_signals":         "DISCORD_LIVE_SIGNALS_WEBHOOK_URL",
    "halt_events":          "DISCORD_HALT_EVENTS_WEBHOOK_URL",
    "fanout_failures":      "DISCORD_FANOUT_FAILURES_WEBHOOK_URL",
    "equity_curve":         "DISCORD_EQUITY_CURVE_WEBHOOK_URL",
    "morning_brief":        "DISCORD_MORNING_BRIEF_WEBHOOK_URL",
    "eod_recap":            "DISCORD_EOD_RECAP_WEBHOOK_URL",
    "status":               "DISCORD_STATUS_WEBHOOK_URL",
    "key_levels":           "DISCORD_KEY_LEVELS_WEBHOOK_URL",
    "news_feed":            "DISCORD_NEWS_FEED_WEBHOOK_URL",
    "pre_market":           "DISCORD_PRE_MARKET_WEBHOOK_URL",
    "trade_journal":        "DISCORD_TRADE_JOURNAL_WEBHOOK_URL",
    "stats_dashboard":      "DISCORD_STATS_DASHBOARD_WEBHOOK_URL",
    "milestones":           "DISCORD_MILESTONES_WEBHOOK_URL",
    "economic_calendar":    "DISCORD_ECONOMIC_CALENDAR_WEBHOOK_URL",
    "concept_of_the_week":  "DISCORD_CONCEPT_OF_THE_WEEK_WEBHOOK_URL",
    "signal_audit":         "DISCORD_SIGNAL_AUDIT_WEBHOOK_URL",
    "bot_logs":             "DISCORD_BOT_LOGS_WEBHOOK_URL",
}

_HTTP_TIMEOUT = 4.0  # seconds — kept short; trading path can't wait on Discord


def _webhook_url(channel: str) -> str:
    env_var = _WEBHOOK_ENV.get(channel)
    if not env_var:
        return ""
    return os.environ.get(env_var, "")


def _post(channel: str, embed: dict, content: str | None = None) -> bool:
    url = _webhook_url(channel)
    if not url:
        return False
    body: dict[str, Any] = {"embeds": [embed]}
    if content:
        body["content"] = content
    try:
        r = requests.post(url, json=body, timeout=_HTTP_TIMEOUT)
        if r.status_code >= 400:
            logger.warning(f"[discord-bridge] {channel} HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.exceptions.RequestException as e:
        logger.warning(f"[discord-bridge] {channel} POST failed: {e}")
        return False


def _now_iso() -> str:
    return datetime.now(tz=EST).isoformat()


def _now_short() -> str:
    return datetime.now(tz=EST).strftime("%H:%M:%S ET")


# ── Live signal ──────────────────────────────────────────────────────────────

def post_signal_executed(
    enriched: Any,
    dispatch: Any,
    fanout_summary: dict | None = None,
) -> bool:
    """Post a fired signal into #live-signals.

    `enriched` is an EnrichedSignal from nova_trading_agents (has action, price,
    sl, tp, grade, sweep, signal_id, ticker, received_at).
    `dispatch` is the DispatchResult (has chosen, attempts).
    `fanout_summary` is the dict returned by subscriber_fanout.fanout_signal.
    """
    action = (enriched.action or "").upper()
    is_long = action in ("BUY", "LONG")
    color = NOVA_GREEN if is_long else NOVA_RED
    title = f"🟢 LONG {enriched.ticker}" if is_long else f"🔴 SHORT {enriched.ticker}"

    fields = [
        {"name": "Entry",  "value": f"{enriched.price:,.2f}", "inline": True},
        {"name": "Stop",   "value": f"{enriched.sl:,.2f}" if enriched.sl else "—", "inline": True},
        {"name": "Target", "value": f"{enriched.tp:,.2f}" if enriched.tp else "—", "inline": True},
    ]
    if getattr(enriched, "grade", None):
        fields.append({"name": "Grade", "value": str(enriched.grade), "inline": True})
    if getattr(enriched, "sweep", None):
        fields.append({"name": "Sweep", "value": str(enriched.sweep), "inline": True})
    if getattr(dispatch, "chosen", None):
        fields.append({"name": "Routed via", "value": str(dispatch.chosen), "inline": True})

    if fanout_summary:
        ok = int(fanout_summary.get("ok", 0))
        total = int(fanout_summary.get("fanned_to", 0))
        fail = int(fanout_summary.get("fail", 0))
        if total > 0:
            line = f"{ok}/{total} subscribers"
            if fail:
                line += f" · {fail} failed"
            fields.append({"name": "Fanout", "value": line, "inline": True})

    received_at = getattr(enriched, "received_at", datetime.now(tz=EST))
    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"signal {enriched.signal_id} · {received_at.strftime('%H:%M:%S ET')}"},
        "timestamp": _now_iso(),
    }
    return _post("live_signals", embed)


def post_signal_failed(enriched: Any, dispatch: Any) -> bool:
    """Dispatch failed — post to #live-signals AND #halt-events."""
    attempts = ", ".join(getattr(a, "message", str(a)) for a in (getattr(dispatch, "attempts", []) or [])) or "—"
    embed = {
        "title": "❗ Signal dispatch failed",
        "color": NOVA_RED,
        "description": "Every venue failed. Trade was NOT placed.",
        "fields": [
            {"name": "Ticker", "value": enriched.ticker, "inline": True},
            {"name": "Action", "value": (enriched.action or "").upper(), "inline": True},
            {"name": "Price",  "value": f"{enriched.price:,.2f}", "inline": True},
            {"name": "Attempts", "value": attempts[:1000], "inline": False},
        ],
        "footer": {"text": f"signal {enriched.signal_id}"},
    }
    _post("live_signals", embed)
    return _post("halt_events", embed)


# ── Halt / risk events ───────────────────────────────────────────────────────

def post_halt(reason: str, state: dict | None = None) -> bool:
    fields: list[dict] = []
    if state:
        if "trades_today" in state:
            fields.append({"name": "Trades today", "value": str(state["trades_today"]), "inline": True})
        if "daily_loss" in state:
            fields.append({"name": "Daily loss", "value": f"${float(state['daily_loss']):,.2f}", "inline": True})
        sess = state.get("session_trades")
        if isinstance(sess, dict):
            for k, v in sess.items():
                fields.append({"name": f"{k} trades", "value": str(v), "inline": True})

    embed = {
        "title": "🛑 NOVA Algo halted",
        "color": NOVA_RED,
        "description": reason[:2000],
        "fields": fields,
        "footer": {"text": f"Activated {datetime.now(tz=EST).strftime('%Y-%m-%d %H:%M:%S ET')}"},
    }
    return _post("halt_events", embed)


def post_gate_rejection(ticker: str, reason: str, gate_state: dict) -> bool:
    """Lighter-weight rejection notice for #halt-events when a gate trips
    (DD limit hit, daily cap reached, session cap reached). Distinct from
    full halt: trading isn't permanently off, just blocked for the rest of
    the day or session.
    """
    fields = [{"name": "Ticker", "value": ticker, "inline": True}]
    if gate_state:
        for k in ("session", "trades_today", "session_trades", "daily_loss", "session_cap"):
            if k in gate_state:
                v = gate_state[k]
                if k == "daily_loss":
                    v = f"${float(v):,.2f}"
                fields.append({"name": k, "value": str(v), "inline": True})

    embed = {
        "title": "⚠ Gate rejection",
        "color": NOVA_AMBER,
        "description": reason[:1000],
        "fields": fields,
        "footer": {"text": _now_short()},
    }
    return _post("halt_events", embed)


# ── Fanout failures ──────────────────────────────────────────────────────────

def post_fanout_failures(failed: list[dict], total: int) -> bool:
    """Post any subscriber webhook delivery failures.

    `failed` is a list of {userId, email, ok, status, body, ...} dicts from
    subscriber_fanout.fanout_signal()['details'] filtered to ok=False.
    """
    if not failed:
        return False

    fail_count = len(failed)
    fields: list[dict] = []
    for f in failed[:10]:
        ident = f.get("email") or f.get("userId") or f.get("label") or "—"
        status = f.get("status")
        body = (f.get("body") or "").strip()
        if status:
            err = f"HTTP {status}"
            if body:
                err += f" · {body[:120]}"
        else:
            err = body or "no response"
        fields.append({"name": str(ident)[:64], "value": err[:1024], "inline": False})
    if fail_count > 10:
        fields.append({"name": "…", "value": f"+{fail_count - 10} more not shown", "inline": False})

    embed = {
        "title": f"⚠ Subscriber fanout — {fail_count}/{total} failed",
        "color": NOVA_AMBER,
        "description": "Subscribers below didn't receive the latest signal.",
        "fields": fields,
        "footer": {"text": _now_short()},
    }
    return _post("fanout_failures", embed)


# ── Equity snapshot ──────────────────────────────────────────────────────────

def post_equity_snapshot(accounts: list[dict], *, day_pnl_total: float | None = None) -> bool:
    """Post a snapshot of all eval accounts to #equity-curve.

    `accounts` is what build_equity_data() returns from app.py:
        [{id, label, current, target, remaining, progress}, ...]
    Optionally pass `day_pnl_total` to show today's net P&L delta.
    """
    if not accounts:
        return False

    fields = []
    total_equity = 0.0
    for a in accounts:
        label = a.get("label", "?")
        cur = float(a.get("current", 0) or 0)
        tgt = float(a.get("target", 0) or 0)
        progress = float(a.get("progress", 0) or 0)
        remaining = float(a.get("remaining", 0) or 0)
        total_equity += cur
        bar_filled = max(0, min(10, int(progress / 10)))
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        fields.append({
            "name": label,
            "value": (
                f"`{bar}` **{progress:.1f}%**\n"
                f"${cur:,.0f} / ${tgt:,.0f}\n"
                f"{remaining:,.0f} to target"
            ),
            "inline": True,
        })

    desc_lines = [f"**Total equity: ${total_equity:,.0f}**"]
    color = NOVA_CYAN
    if day_pnl_total is not None:
        arrow = "🟢" if day_pnl_total >= 0 else "🔴"
        desc_lines.append(f"{arrow} ${day_pnl_total:+,.0f} today")
        color = NOVA_GREEN if day_pnl_total >= 0 else NOVA_RED

    embed = {
        "title": "📈 NOVA Algo — equity snapshot",
        "color": color,
        "description": "\n".join(desc_lines),
        "fields": fields,
        "footer": {"text": datetime.now(tz=EST).strftime("%a %b %d, %Y · %H:%M ET")},
    }
    return _post("equity_curve", embed)


# ── Morning brief ────────────────────────────────────────────────────────────

def post_morning_brief(
    *,
    bias: str | None = None,
    levels: dict | None = None,
    conditions: str | None = None,
    notes: str | None = None,
) -> bool:
    fields: list[dict] = []
    if bias:
        fields.append({"name": "Bias", "value": bias, "inline": True})
    if levels:
        for k, v in list(levels.items())[:12]:
            fields.append({"name": str(k), "value": str(v), "inline": True})
    if conditions:
        fields.append({"name": "Conditions", "value": conditions[:1000], "inline": False})

    embed = {
        "title": "🌅 NY AM Morning Brief",
        "color": NOVA_CYAN,
        "description": (notes or "NY AM ORB setup. NQ futures, 15m execution.")[:2000],
        "fields": fields,
        "footer": {"text": f"{datetime.now(tz=EST).strftime('%a %b %d')} · session opens 8:30am ET"},
    }
    return _post("morning_brief", embed)


# ── EOD recap ────────────────────────────────────────────────────────────────

def post_eod_recap(
    *,
    trades_today: int,
    wins: int = 0,
    losses: int = 0,
    breakeven: int = 0,
    day_pnl: float = 0.0,
    notes: str | None = None,
    last_signal: dict | None = None,
    equity: list[dict] | None = None,
    open_positions: dict | None = None,
    pipeline_note: str | None = None,
) -> bool:
    """
    Rich EOD recap. Mirrors the narrative produced by post_eod_recap.py.

    last_signal:    {"action", "ticker", "price", "recorded_at"} for headline
    equity:         build_equity_data() output — used for fleet snapshot block
    open_positions: state["open_positions"] — used for "open remaining"
    pipeline_note:  optional override for pipeline-status block
    """
    now_et = datetime.now(tz=EST)
    today_str = now_et.strftime("%A · %B ") + str(now_et.day) + now_et.strftime(", %Y")
    title = f"📊 EOD Recap · {today_str}"

    if trades_today and trades_today > 0:
        # Active day — narrative recap
        action = (last_signal or {}).get("action", "?").upper()
        ticker = (last_signal or {}).get("ticker", "NQ1!")
        entry_price = (last_signal or {}).get("price", 0)
        recorded_at = (last_signal or {}).get("recorded_at", "") or ""
        fire_time = recorded_at[11:16] + " ET" if len(recorded_at) >= 16 else "—"

        color = NOVA_GREEN if day_pnl >= 0 else NOVA_RED
        arrow = "🟢" if day_pnl >= 0 else "🔴"
        description = (
            f"**NOVA fired {arrow} {day_pnl:+,.0f} on the day.**\n"
            f"{action} {ticker} @ {entry_price} at {fire_time} · NY AM 9:30–11:00 ET · NQ · 30m"
        )

        fields: list[dict] = []

        outcome_value_lines = [
            f"**Founder fleet: ${day_pnl:+,.0f}** net across all live accounts.",
        ]
        outcome_value_lines.append(
            f"Open positions remaining: **{len(open_positions or {})}** · "
            f"Wins/Losses/BE: {wins}/{losses}/{breakeven}"
        )
        fields.append({
            "name": "📈 Today",
            "value": "\n".join(outcome_value_lines),
            "inline": False,
        })

        if equity:
            eq_lines = []
            total_current = 0.0
            total_target = 0.0
            for acct in equity:
                cur = acct.get("current", 0)
                tgt = acct.get("target", 0)
                prog = acct.get("progress", 0)
                lbl = acct.get("label", acct.get("id", "?"))
                total_current += cur
                total_target += tgt
                eq_lines.append(f"• **{lbl}** — ${cur:,.2f} ({prog:.1f}% to ${tgt:,.0f})")
            fields.append({
                "name": "💰 Founder fleet equity",
                "value": "\n".join(eq_lines),
                "inline": False,
            })
            combined_pct = (total_current / total_target * 100) if total_target else 0
            fields.append({
                "name": "🎯 Combined progress",
                "value": f"${total_current:,.2f} of ${total_target:,.0f} target ({combined_pct:.1f}%)",
                "inline": False,
            })

        if pipeline_note:
            fields.append({
                "name": "🛠️ Pipeline status",
                "value": pipeline_note[:1000],
                "inline": False,
            })

        if notes:
            fields.append({"name": "Notes", "value": notes[:1000], "inline": False})

        fields.append({
            "name": "🔗 Verify everything",
            "value": (
                "📈 [novaalgo.org/performance](https://novaalgo.org/performance) · live stats\n"
                "🟢 [novaalgo.org/status](https://novaalgo.org/status) · system health\n"
                "📓 [/portal/journal](https://novaalgo.org/portal/journal) · your fills"
            ),
            "inline": False,
        })

        embed = {
            "title": title,
            "color": color,
            "description": description,
            "fields": fields,
            "footer": {"text": "NOVA Algo · NY AM ORB · NQ futures · auto-routed"},
        }
    else:
        # No-fire day
        embed = {
            "title": title,
            "color": NOVA_CYAN,
            "description": (
                "**No fire today** — price stayed inside the Opening Range during the "
                "trade window. NOVA does not force trades. ~30% of NY AM days resolve "
                "this way. We wait for clean breakouts."
            ),
            "fields": [],
            "footer": {"text": "NOVA Algo · NY AM ORB · NQ futures · auto-routed"},
        }

        if equity:
            eq_lines = []
            for acct in equity:
                cur = acct.get("current", 0)
                lbl = acct.get("label", "?")
                prog = acct.get("progress", 0)
                eq_lines.append(f"• {lbl} — ${cur:,.2f} ({prog:.1f}% to target)")
            embed["fields"].append({
                "name": "💰 Founder fleet equity (unchanged)",
                "value": "\n".join(eq_lines),
                "inline": False,
            })

        embed["fields"].append({
            "name": "🔗 Verify everything",
            "value": (
                "📈 [novaalgo.org/performance](https://novaalgo.org/performance) · live stats\n"
                "🟢 [novaalgo.org/status](https://novaalgo.org/status) · system health"
            ),
            "inline": False,
        })

    return _post("eod_recap", embed)


# ── Key levels (pre-market 7:30 ET) ──────────────────────────────────────────

def post_key_levels(levels: dict) -> bool:
    """
    Post the day's key reference levels for NQ futures into #key-levels.

    levels dict shape:
      {
        "as_of":         "Wed Apr 30, 2026",
        "symbol":        "NQ1!",
        "current":       21500.50,    # last close
        "pdh":           21620.00,    # prior day high
        "pdl":           21430.00,    # prior day low
        "weekly_open":   21505.25,    # Sunday 6pm ET open
        "prior_week_h":  21750.00,
        "prior_week_l":  21280.00,
        "session_h_5d":  21750.00,    # 5-day session high
        "session_l_5d":  21280.00,    # 5-day session low
      }
    """
    title = f"🎯 Key Levels · NQ · {levels.get('as_of', _now_short())}"
    cur = levels.get("current")
    rows = []
    pdh = levels.get("pdh")
    pdl = levels.get("pdl")
    if pdh is not None: rows.append(f"**PDH** · {pdh:,.2f}")
    if pdl is not None: rows.append(f"**PDL** · {pdl:,.2f}")
    if levels.get("weekly_open") is not None:
        rows.append(f"**Weekly open** · {levels['weekly_open']:,.2f}")
    if levels.get("prior_week_h") is not None:
        rows.append(f"**Prior week high** · {levels['prior_week_h']:,.2f}")
    if levels.get("prior_week_l") is not None:
        rows.append(f"**Prior week low** · {levels['prior_week_l']:,.2f}")
    if levels.get("session_h_5d") is not None:
        rows.append(f"**5-day high** · {levels['session_h_5d']:,.2f}")
    if levels.get("session_l_5d") is not None:
        rows.append(f"**5-day low** · {levels['session_l_5d']:,.2f}")

    desc_lines = []
    if cur is not None:
        desc_lines.append(f"**Last** · {cur:,.2f}")
    if pdh is not None and pdl is not None and cur is not None:
        if cur > pdh:
            desc_lines.append("↗ above PDH — bullish bias zone")
        elif cur < pdl:
            desc_lines.append("↘ below PDL — bearish bias zone")
        else:
            desc_lines.append("⇄ inside PDR — neutral, breakout watch")

    embed = {
        "title": title,
        "color": NOVA_CYAN,
        "description": "\n".join(desc_lines) or "Reference levels for today's NY AM ORB.",
        "fields": [
            {"name": "📍 Reference levels", "value": "\n".join(rows) or "—", "inline": False},
            {"name": "⏰ Trade window", "value": "9:30 – 11:00 ET · 30m timeframe · NQ futures", "inline": False},
        ],
        "footer": {"text": "NOVA Algo · NY AM ORB · auto-pushed daily 7:30 ET"},
    }
    return _post("key_levels", embed)


# ── News feed (macro events 7:00 ET) ─────────────────────────────────────────

def post_news_feed(events: list[dict]) -> bool:
    """
    Post today's high-impact macro events into #news-feed.

    events shape:
      [{ "time": "08:30 ET", "title": "Initial Jobless Claims", "currency": "USD",
         "impact": "high", "forecast": "215K", "previous": "212K" }, ...]
    """
    today_str = datetime.now(tz=EST).strftime("%A · %B %d")
    if not events:
        embed = {
            "title": f"📰 Macro feed · {today_str}",
            "color": NOVA_CYAN,
            "description": "**No high-impact USD events today.** Pure technicals — clean ORB read.",
            "footer": {"text": "NOVA Algo · auto-pushed daily 7:00 ET"},
        }
        return _post("news_feed", embed)

    rows = []
    for e in events[:12]:
        impact = (e.get("impact") or "").lower()
        icon = "🔴" if impact == "high" else ("🟡" if impact == "medium" else "🟢")
        time_str = e.get("time", "—")
        title = e.get("title", "Event")
        cur = e.get("currency", "")
        forecast = e.get("forecast")
        previous = e.get("previous")
        line = f"{icon} **{time_str}** · {title}"
        if cur:
            line += f" ({cur})"
        bits = []
        if forecast:
            bits.append(f"forecast {forecast}")
        if previous:
            bits.append(f"prior {previous}")
        if bits:
            line += " · " + " · ".join(bits)
        rows.append(line)

    embed = {
        "title": f"📰 Macro feed · {today_str}",
        "color": NOVA_AMBER,
        "description": (
            f"**{len([e for e in events if (e.get('impact') or '').lower() == 'high'])} high-impact** · "
            f"{len(events)} total events flagged for today.\n"
            f"Watch for whipsaw if a print lands inside the 9:30–11:00 ET window."
        ),
        "fields": [
            {"name": "🗓 Today's prints", "value": "\n".join(rows)[:1000], "inline": False},
        ],
        "footer": {"text": "NOVA Algo · auto-pushed daily 7:00 ET"},
    }
    return _post("news_feed", embed)


# ── Pre-market snapshot (9:00 ET) ────────────────────────────────────────────

def post_pre_market(snapshot: dict) -> bool:
    """
    Post the 9:00 ET pre-market read into #pre-market.

    snapshot shape:
      {
        "as_of":      "Wed Apr 30, 9:00 AM ET",
        "current":    21500.50,
        "pdh":        21620.00,
        "pdl":        21430.00,
        "weekly_open":21505.25,
        "news_count": 2,
        "next_event": {"time":"10:00 ET","title":"ISM Manufacturing PMI"},
        "expected_or_width": 80.0,   # rough from rolling 20d
      }
    """
    title = f"☕ Pre-market · {snapshot.get('as_of', _now_short())}"
    cur = snapshot.get("current")
    pdh = snapshot.get("pdh")
    pdl = snapshot.get("pdl")

    bias_line = "—"
    if cur is not None and pdh is not None and pdl is not None:
        if cur > pdh:
            bias_line = "**Above PDH** · upside continuation watch into open"
        elif cur < pdl:
            bias_line = "**Below PDL** · downside continuation watch into open"
        else:
            mid = (pdh + pdl) / 2
            bias_line = (
                "**Inside PDR · upper half** · slight bull lean" if cur > mid
                else "**Inside PDR · lower half** · slight bear lean"
            )

    fields = [
        {"name": "🎯 Bias going in", "value": bias_line, "inline": False},
    ]

    levels_rows = []
    if cur is not None: levels_rows.append(f"Last · {cur:,.2f}")
    if pdh is not None: levels_rows.append(f"PDH · {pdh:,.2f}")
    if pdl is not None: levels_rows.append(f"PDL · {pdl:,.2f}")
    if snapshot.get("weekly_open") is not None:
        levels_rows.append(f"Weekly open · {snapshot['weekly_open']:,.2f}")
    if levels_rows:
        fields.append({"name": "📍 Levels", "value": "\n".join(levels_rows), "inline": True})

    if snapshot.get("expected_or_width"):
        fields.append({
            "name": "📐 Expected OR width",
            "value": f"~{snapshot['expected_or_width']:.0f} pts (20d avg)",
            "inline": True,
        })

    next_event = snapshot.get("next_event") or {}
    if next_event.get("title"):
        fields.append({
            "name": "📰 Next macro print",
            "value": f"**{next_event.get('time','')}** · {next_event['title']}",
            "inline": False,
        })

    embed = {
        "title": title,
        "color": NOVA_CYAN,
        "description": (
            "30 minutes to the bell. NOVA arms triggers at 10:00 ET — "
            "buy-stop above OR high, sell-stop below OR low. First side wins."
        ),
        "fields": fields,
        "footer": {"text": "NOVA Algo · pre-market read · auto-pushed daily 9:00 ET"},
    }
    return _post("pre_market", embed)


# ── Trade journal (auto post-mortem on close) ────────────────────────────────

def post_trade_journal(close: dict) -> bool:
    """
    Post a single trade post-mortem into #trade-journal when a position closes.

    close dict shape:
      {
        "ticker":     "NQ1!",
        "side":       "buy" | "sell",
        "entry":      21500.00,
        "exit":       21580.00,
        "exit_reason":"TP" | "SL" | "BEExit" | "TrailExit" | "SessionFlat",
        "r_multiple": 4.0,           # signed: +4 win, -1 loss
        "usd_pnl":    2000.00,       # signed
        "hold_min":   42,
        "opened_at":  "2026-04-30T09:30:00-04:00",
        "closed_at":  "2026-04-30T10:12:00-04:00",
      }
    """
    side = (close.get("side") or "").lower()
    is_long = side in ("buy", "long")
    arrow = "🟢" if (close.get("usd_pnl", 0) or 0) >= 0 else "🔴"
    color = NOVA_GREEN if (close.get("usd_pnl", 0) or 0) >= 0 else NOVA_RED

    reason = close.get("exit_reason", "—")
    title = f"{arrow} {'LONG' if is_long else 'SHORT'} {close.get('ticker','NQ1!')} · {reason}"

    pnl = close.get("usd_pnl", 0) or 0
    r = close.get("r_multiple", 0) or 0
    desc = f"**{pnl:+,.0f}** USD · **{r:+.1f}R** · {close.get('hold_min','—')} min hold"

    fields = [
        {"name": "Entry", "value": f"{close.get('entry','—'):,.2f}" if isinstance(close.get('entry'), (int, float)) else str(close.get('entry','—')), "inline": True},
        {"name": "Exit",  "value": f"{close.get('exit','—'):,.2f}" if isinstance(close.get('exit'), (int, float)) else str(close.get('exit','—')), "inline": True},
        {"name": "Reason", "value": reason, "inline": True},
    ]
    if close.get("opened_at"):
        fields.append({"name": "Opened", "value": close["opened_at"][11:16] + " ET", "inline": True})
    if close.get("closed_at"):
        fields.append({"name": "Closed", "value": close["closed_at"][11:16] + " ET", "inline": True})

    embed = {
        "title": title,
        "color": color,
        "description": desc,
        "fields": fields,
        "footer": {"text": "NOVA Algo · auto post-mortem · NY AM ORB"},
    }
    return _post("trade_journal", embed)


# ── Stats dashboard (nightly merged stats card) ──────────────────────────────

def post_stats_dashboard(stats: dict) -> bool:
    """
    Post merged backtest + live forward stats into #stats-dashboard.

    stats shape (matches /api/stats/live merged block):
      {"trades": 324, "winRate": 82.72, "profitFactor": 7.82,
       "netUsd": 156625, "netPerTrade": 483, "asOf": "2026-04-29T..."}
    """
    embed = {
        "title": "📊 NOVA Algo · live performance",
        "color": NOVA_GREEN,
        "description": (
            f"**{stats.get('trades', 0):,}** trades · "
            f"**{stats.get('winRate', 0):.2f}%** win rate · "
            f"**PF {stats.get('profitFactor', 0):.2f}**"
        ),
        "fields": [
            {"name": "Net",          "value": f"+${stats.get('netUsd', 0):,.0f}",       "inline": True},
            {"name": "Net per trade","value": f"+${stats.get('netPerTrade', 0):,.0f}",  "inline": True},
            {"name": "Strategy",     "value": "NY AM ORB · 30m · NQ",                   "inline": True},
            {"name": "Verify",       "value": "[novaalgo.org/performance](https://novaalgo.org/performance)", "inline": False},
        ],
        "footer": {"text": "NOVA Algo · auto-pushed nightly · backtest + live forward"},
    }
    return _post("stats_dashboard", embed)


# ── Economic calendar (weekly digest) ────────────────────────────────────────

def post_economic_calendar(week_events: list[dict], *, week_label: str = "") -> bool:
    """
    Post the upcoming week's high/medium-impact USD events to #economic-calendar.

    week_events: same shape as post_news_feed events, but spans 5 days.
    Grouped by date in the rendering.
    """
    if not week_events:
        embed = {
            "title": f"🗓 Macro week · {week_label}".rstrip(" ·"),
            "color": NOVA_CYAN,
            "description": "**No high-impact USD prints this week.** Pure technicals — clean ORB read all five days.",
            "footer": {"text": "NOVA Algo · auto-pushed Sunday 6pm ET"},
        }
        return _post("economic_calendar", embed)

    # Group by weekday from event["time_iso"] if present, else flat list
    by_day: dict[str, list[dict]] = {}
    for ev in week_events:
        day = ev.get("date", ev.get("day", "this week"))
        by_day.setdefault(day, []).append(ev)

    fields = []
    for day, events in by_day.items():
        rows = []
        for e in events[:8]:
            impact = (e.get("impact") or "").lower()
            icon = "🔴" if impact == "high" else ("🟡" if impact == "medium" else "🟢")
            t = e.get("time", "—")
            rows.append(f"{icon} **{t}** · {e.get('title','Event')}")
        fields.append({"name": f"📅 {day}", "value": "\n".join(rows)[:1000], "inline": False})

    high = sum(1 for e in week_events if (e.get("impact") or "").lower() == "high")
    embed = {
        "title": f"🗓 Macro week · {week_label}".rstrip(" ·"),
        "color": NOVA_AMBER,
        "description": f"**{high} high-impact** prints across {len(by_day)} days. Plan accordingly — NOVA fires inside 9:30-11:00 ET.",
        "fields": fields[:8],
        "footer": {"text": "NOVA Algo · auto-pushed Sunday 6pm ET"},
    }
    return _post("economic_calendar", embed)


# ── Milestones (event-triggered) ─────────────────────────────────────────────

def post_milestone(*, kind: str, title: str, body: str = "", color: int | None = None) -> bool:
    """
    kind: 'account_flip' | 'beta_seat' | 'tier_upgrade' | 'first_fire' | 'streak'
    """
    icons = {
        "account_flip":  "🏆",
        "beta_seat":     "🪑",
        "tier_upgrade":  "📈",
        "first_fire":    "🎯",
        "streak":        "🔥",
    }
    icon = icons.get(kind, "✨")
    embed = {
        "title": f"{icon} {title}",
        "color": color or NOVA_GREEN,
        "description": body[:1800],
        "footer": {"text": f"NOVA Algo · {kind}"},
    }
    return _post("milestones", embed)


# ── Signal audit (per-webhook mirror, staff) ─────────────────────────────────

def post_signal_audit(*, payload: dict, gates: dict | None = None, dispatch_result: dict | None = None) -> bool:
    """Mirror every incoming /webhook event into #signal-audit for staff visibility."""
    action = (payload.get("action") or "").upper()
    ticker = payload.get("ticker", "?")
    price  = payload.get("price")
    fields = [
        {"name": "Action",  "value": action or "—", "inline": True},
        {"name": "Ticker",  "value": str(ticker),    "inline": True},
        {"name": "Price",   "value": f"{price:,.2f}" if isinstance(price, (int, float)) else str(price or "—"), "inline": True},
    ]
    if gates:
        gate_summary = " · ".join(f"{k}={v}" for k, v in list(gates.items())[:6])
        fields.append({"name": "Gates", "value": gate_summary[:1000] or "—", "inline": False})
    if dispatch_result:
        fields.append({
            "name": "Dispatch",
            "value": f"primary={dispatch_result.get('primary_ok','?')} · fanned_to={dispatch_result.get('fanned_to',0)} · ok={dispatch_result.get('ok',0)} · fail={dispatch_result.get('fail',0)}",
            "inline": False,
        })
    embed = {
        "title": f"🔍 Webhook · {action} {ticker}",
        "color": NOVA_AMBER if action == "EXIT" else NOVA_CYAN,
        "fields": fields,
        "footer": {"text": _now_short() + " · staff audit"},
    }
    return _post("signal_audit", embed)


# ── Concept of the week ──────────────────────────────────────────────────────

def post_concept_of_the_week(*, title: str, body: str, takeaway: str | None = None,
                             week_label: str | None = None) -> bool:
    """Post the weekly education concept into #concept-of-the-week."""
    label = week_label or datetime.now(tz=EST).strftime("Week of %b %d")
    fields = []
    if takeaway:
        fields.append({"name": "🎯 The takeaway", "value": takeaway[:1000], "inline": False})
    embed = {
        "title": f"🧠 Concept of the Week · {label}",
        "color": NOVA_CYAN,
        "description": (f"**{title}**\n\n{body}")[:3800],
        "fields": fields,
        "footer": {"text": "NOVA Algo · weekly education · drop questions in the thread"},
    }
    return _post("concept_of_the_week", embed)


# ── Status / heartbeat ───────────────────────────────────────────────────────

def post_status_heartbeat(snapshot: dict) -> bool:
    """Hourly Railway health heartbeat to #status. Distinct from post_status (events)."""
    session = snapshot.get("active_session") or "—"
    trades  = snapshot.get("trades_today", 0)
    loss    = snapshot.get("daily_loss", 0)
    cap     = snapshot.get("loss_limit", 500)
    open_n  = len(snapshot.get("open_positions") or {})
    fanout_count = snapshot.get("approved_subscribers", 0)
    color = NOVA_GREEN
    if loss >= cap:
        color = NOVA_RED
    elif loss > 0 or open_n > 0:
        color = NOVA_AMBER

    embed = {
        "title": "🟢 NOVA Algo · heartbeat",
        "color": color,
        "description": f"**Session:** {session}",
        "fields": [
            {"name": "Trades today",   "value": str(trades),                  "inline": True},
            {"name": "Loss budget",    "value": f"${loss:.0f} / ${cap:.0f}",  "inline": True},
            {"name": "Open positions", "value": str(open_n),                   "inline": True},
            {"name": "Approved subs",  "value": str(fanout_count),             "inline": True},
        ],
        "footer": {"text": _now_short() + " · NOVA Algo"},
    }
    return _post("status", embed)


def post_status(
    message: str,
    *,
    level: str = "info",
    fields: list[dict] | None = None,
) -> bool:
    icons = {"info": "ℹ️", "ok": "✅", "warn": "⚠", "error": "❗"}
    colors = {"info": NOVA_CYAN, "ok": NOVA_GREEN, "warn": NOVA_AMBER, "error": NOVA_RED}
    title_msg = message[:100]
    desc = message[100:1900] if len(message) > 100 else None

    embed = {
        "title": f"{icons.get(level, 'ℹ️')} {title_msg}",
        "color": colors.get(level, NOVA_CYAN),
        "footer": {"text": _now_short()},
    }
    if desc:
        embed["description"] = desc
    if fields:
        embed["fields"] = fields[:10]
    return _post("status", embed)


# ── Smoke-test ───────────────────────────────────────────────────────────────

def smoke_test() -> dict:
    """Fire one test post per configured channel. Returns dict of channel→bool."""
    results = {}
    results["status"] = post_status(
        "NOVA Algo bridge online — smoke test",
        level="ok",
        fields=[{"name": "Origin", "value": "smoke_test()", "inline": True}],
    )
    results["live_signals"] = _post("live_signals", {
        "title": "🔔 Bridge connectivity test",
        "color": NOVA_CYAN,
        "description": "If you see this, Railway → Discord live-signals works.",
        "footer": {"text": _now_short()},
    })
    results["halt_events"] = _post("halt_events", {
        "title": "🛑 Bridge connectivity test",
        "color": NOVA_AMBER,
        "description": "If you see this, halt-events pipe works.",
        "footer": {"text": _now_short()},
    })
    results["fanout_failures"] = _post("fanout_failures", {
        "title": "⚠ Bridge connectivity test",
        "color": NOVA_AMBER,
        "description": "If you see this, fanout-failures pipe works.",
        "footer": {"text": _now_short()},
    })
    results["equity_curve"] = post_equity_snapshot(
        [
            {"label": "Apex 100K (test)", "current": 99532.60, "target": 106000.00, "progress": 81.3, "remaining": 6467.40},
            {"label": "Lucid 50K (test)", "current": 50000.00, "target": 53000.00, "progress": 16.7, "remaining": 3000.00},
        ],
        day_pnl_total=0.0,
    )
    results["morning_brief"] = post_morning_brief(
        bias="Long-bias above PDH",
        levels={"PDH": "21505", "PDL": "21380", "Asia high": "21472"},
        conditions="Clean continuation tape; CPI risk at 8:30 ET.",
        notes="Smoke test — not a real brief.",
    )
    results["eod_recap"] = post_eod_recap(
        trades_today=0, wins=0, losses=0, day_pnl=0.0,
        notes="Smoke test — no real session data.",
    )
    return results


if __name__ == "__main__":
    # Allows: `python nova_algo_discord_bridge.py` to run smoke test locally
    # using whatever DISCORD_*_WEBHOOK_URL env vars are set.
    from dotenv import load_dotenv
    load_dotenv()
    print("Running smoke test...")
    out = smoke_test()
    for ch, ok in out.items():
        print(f"  {'✓' if ok else '✗'} {ch}")
