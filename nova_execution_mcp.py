"""
NOVA Execution MCP Server.

Exposes live trade execution as MCP tools for Claude Code.
All tools default to dry_run=True — flipping a trade live requires explicit
dry_run=False. Every call routes through the Railway /execute and /close
endpoints so server-side session/DD/daily-loss gates apply.

Architecture:
  Claude Code (stdio) <-> this MCP <-> Railway app.py <-> TradersPost <-> broker

Register in ~/.claude.json under mcpServers:
  "nova-execution": {
    "type": "stdio",
    "command": "python",
    "args": ["C:/Users/User/nova/nova_execution_mcp.py"],
    "env": {}
  }

Run standalone for smoke test:
  python nova_execution_mcp.py --self-test
"""
from __future__ import annotations

import os
import sys
import json
import requests
from typing import Any
from mcp.server.fastmcp import FastMCP

# ── Config ────────────────────────────────────────────────────────────────────
NOVA_API_URL = os.environ.get(
    "NOVA_API_URL",
    "https://nova-production-72f5.up.railway.app",
).rstrip("/")

HTTP_TIMEOUT = 10  # seconds

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP("nova-execution")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(path: str, payload: dict) -> dict:
    """POST to NOVA API and return JSON response (or an error dict)."""
    url = f"{NOVA_API_URL}{path}"
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text}
        return {"http_status": r.status_code, "url": url, **body}
    except requests.RequestException as e:
        return {"http_status": 0, "url": url, "status": "error", "message": str(e)}


def _get(path: str) -> dict:
    url = f"{NOVA_API_URL}{path}"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text}
        return {"http_status": r.status_code, "url": url, **body}
    except requests.RequestException as e:
        return {"http_status": 0, "url": url, "status": "error", "message": str(e)}


# ── Read tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def nova_status() -> dict:
    """Get NOVA trading server status: current session, trade counts, daily loss,
    open positions, last signal, and equity across all 3 prop accounts.

    Returns the full /status payload from the Railway server.
    """
    return _get("/status")


@mcp.tool()
def nova_positions() -> dict:
    """List every tracked open position on the NOVA server. Each position shows
    entry price, side, SL, TP, BE, grade, sweep type, session, and opened_at
    timestamp. Use this before calling nova_close to confirm what's live.
    """
    return _get("/positions")


@mcp.tool()
def nova_equity() -> dict:
    """Get current equity, target, progress %, and dollars remaining across
    Apex 50K, Apex 100K, and Lucid 50K prop accounts.
    """
    return _get("/equity")


# ── Write tools (dry-run default) ────────────────────────────────────────────

@mcp.tool()
def nova_open_long(
    ticker: str,
    price: float,
    sl: float,
    tp: float,
    grade: str = "A",
    be: float | None = None,
    sweep: str = "",
    comment: str = "Manual long from Claude",
    dry_run: bool = True,
    force: bool = False,
) -> dict:
    """Open a LONG position through NOVA's Railway → TradersPost pipeline.

    IMPORTANT: dry_run defaults to True. Pass dry_run=False to fire a live order.
    Server-side gates still apply: session window, max 1 trade/session, max 3/day,
    daily loss cap $500, grade must be A or A+ unless force=True.

    Args:
      ticker:  Instrument symbol (e.g., "NQ1!", "XAUUSD")
      price:   Entry price
      sl:      Stop loss
      tp:      Take profit
      grade:   "A+" | "A" | "B" | "C"  (B/C rejected unless force=True)
      be:      Optional breakeven trigger price
      sweep:   Optional sweep label (e.g., "PDL", "AR_Low", "Swing_Low")
      comment: Free-form comment written to TradersPost + Obsidian log
      dry_run: If True, reports what WOULD happen without firing (default True)
      force:   If True, bypass grade filter (still subject to session/DD gates)
    """
    payload = {
        "ticker":  ticker,
        "action":  "buy",
        "price":   float(price),
        "sl":      float(sl),
        "tp":      float(tp),
        "grade":   grade,
        "comment": comment,
        "dry_run": bool(dry_run),
        "force":   bool(force),
    }
    if be is not None:
        payload["be"] = float(be)
    if sweep:
        payload["sweep"] = sweep
    return _post("/execute", payload)


@mcp.tool()
def nova_open_short(
    ticker: str,
    price: float,
    sl: float,
    tp: float,
    grade: str = "A",
    be: float | None = None,
    sweep: str = "",
    comment: str = "Manual short from Claude",
    dry_run: bool = True,
    force: bool = False,
) -> dict:
    """Open a SHORT position through NOVA's Railway → TradersPost pipeline.

    IMPORTANT: dry_run defaults to True. Pass dry_run=False to fire a live order.
    Same gates as nova_open_long: session window, session/day limits, daily loss
    cap, grade filter (A or A+ unless force=True).
    """
    payload = {
        "ticker":  ticker,
        "action":  "sell",
        "price":   float(price),
        "sl":      float(sl),
        "tp":      float(tp),
        "grade":   grade,
        "comment": comment,
        "dry_run": bool(dry_run),
        "force":   bool(force),
    }
    if be is not None:
        payload["be"] = float(be)
    if sweep:
        payload["sweep"] = sweep
    return _post("/execute", payload)


@mcp.tool()
def nova_close(
    ticker: str,
    dry_run: bool = True,
    outcome: str = "",
    exit_price: float | None = None,
    accounts: int | None = None,
    comment: str = "Manual close from Claude",
) -> dict:
    """Close an open position for a ticker via TradersPost.

    IMPORTANT: dry_run defaults to True. Pass dry_run=False to fire a live close.
    Close is the safety valve — permitted regardless of session/grade/limits.

    Args:
      ticker:     Instrument symbol to close (e.g., "NQ1!")
      dry_run:    If True, reports what WOULD happen without firing (default True)
      outcome:    Optional — "win" | "loss" | "be" — updates Obsidian trade log
                  and (for loss) adds RISK_PER_TRADE × accounts to daily_loss
      exit_price: Optional exit price, used with outcome to update trade log
      accounts:   Number of copy-traded accounts this signal filled on. If a
                  losing trade fanned out to all 3 accounts, pass accounts=3
                  so the server books the real $1,500 hit instead of $500.
                  Defaults to the server's ACTIVE_ACCOUNTS (currently 3).
      comment:    Free-form comment for the close
    """
    payload: dict[str, Any] = {
        "ticker":  ticker,
        "dry_run": bool(dry_run),
        "comment": comment,
    }
    if outcome:
        payload["outcome"] = outcome
    if exit_price is not None:
        payload["exit_price"] = float(exit_price)
    if accounts is not None:
        payload["accounts"] = int(accounts)
    return _post("/close", payload)


@mcp.tool()
def nova_report_result(
    outcome: str,
    exit_price: float,
    ticker: str = "",
    accounts: int | None = None,
) -> dict:
    """Report the outcome of a closed trade WITHOUT firing a close order (the
    position is already closed — this just updates the trade log + daily_loss).

    Use this when the trade closed at the broker (SL/TP hit, manual close on
    mobile, etc.) and you need the NOVA server state to reflect reality.

    Args:
      outcome:    "win" | "loss" | "be"
      exit_price: Price at which the position closed
      ticker:     Optional ticker — if provided, clears open_positions tracker
      accounts:   Number of copy-traded accounts this signal filled on. On a
                  loss, server deducts RISK_PER_TRADE × accounts from the
                  daily-loss budget. Defaults to ACTIVE_ACCOUNTS (currently 3).
    """
    payload: dict[str, Any] = {
        "outcome":    outcome,
        "exit_price": float(exit_price),
    }
    if ticker:
        payload["ticker"] = ticker
    if accounts is not None:
        payload["accounts"] = int(accounts)
    return _post("/report-result", payload)


# ── Self-test path ────────────────────────────────────────────────────────────

def _self_test() -> None:
    print(f"NOVA API URL: {NOVA_API_URL}")
    print("\n[1/3] GET /status ...")
    print(json.dumps(_get("/status"), indent=2, default=str))
    print("\n[2/3] GET /positions ...")
    print(json.dumps(_get("/positions"), indent=2, default=str))
    print("\n[3/3] POST /execute (dry_run, grade=A) ...")
    out = _post("/execute", {
        "ticker":  "NQ1!",
        "action":  "buy",
        "price":   21500.00,
        "sl":      21475.00,
        "tp":      21550.00,
        "be":      21525.00,
        "grade":   "A",
        "sweep":   "PDL",
        "comment": "self-test",
        "dry_run": True,
    })
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        mcp.run()
