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
    "live_signals":    "DISCORD_LIVE_SIGNALS_WEBHOOK_URL",
    "halt_events":     "DISCORD_HALT_EVENTS_WEBHOOK_URL",
    "fanout_failures": "DISCORD_FANOUT_FAILURES_WEBHOOK_URL",
    "equity_curve":    "DISCORD_EQUITY_CURVE_WEBHOOK_URL",
    "morning_brief":   "DISCORD_MORNING_BRIEF_WEBHOOK_URL",
    "eod_recap":       "DISCORD_EOD_RECAP_WEBHOOK_URL",
    "status":          "DISCORD_STATUS_WEBHOOK_URL",
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
    wins: int,
    losses: int,
    breakeven: int = 0,
    day_pnl: float = 0.0,
    notes: str | None = None,
) -> bool:
    arrow = "🟢" if day_pnl >= 0 else "🔴"
    color = NOVA_GREEN if day_pnl >= 0 else NOVA_RED
    win_rate = f"{(wins / max(1, wins + losses)) * 100:.0f}%" if (wins + losses) else "—"

    fields = [
        {"name": "Trades", "value": str(trades_today), "inline": True},
        {"name": "Wins",   "value": str(wins), "inline": True},
        {"name": "Losses", "value": str(losses), "inline": True},
        {"name": "Breakeven", "value": str(breakeven), "inline": True},
        {"name": "Win rate", "value": win_rate, "inline": True},
        {"name": "Net", "value": f"${day_pnl:+,.0f}", "inline": True},
    ]
    if notes:
        fields.append({"name": "Notes", "value": notes[:1000], "inline": False})

    embed = {
        "title": "🌇 EOD Recap",
        "color": color,
        "description": f"{arrow} **${day_pnl:+,.0f}** today",
        "fields": fields,
        "footer": {"text": datetime.now(tz=EST).strftime("%a %b %d, %Y")},
    }
    return _post("eod_recap", embed)


# ── Status / heartbeat ───────────────────────────────────────────────────────

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
