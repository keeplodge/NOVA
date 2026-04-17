"""
nova_evolution_agent.py — Strategy parameter evolution.

Reviews all accumulated trade data and uses Claude to suggest specific
Pine Script parameter changes to improve edge over time.
"""
import os, json
from datetime import datetime
from anthropic import Anthropic
from nova_trade_db import get_trades, get_stats
from nova_pattern_agent import compute_breakdowns

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OBSIDIAN_DIR      = r"C:\Users\User\nova\nova-brain\04_Strategy"
PINE_PATH         = r"C:\Users\User\nova\nova_ict_strategy.pine"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

CURRENT_PARAMS = {
    "i_swing":      5,      # swing pivot length
    "i_bpd":        288,    # PDH/L lookback bars (288 = 1 day on 5m)
    "i_buf":        2.0,    # SL buffer in points
    "i_rr":         2.0,    # R:R ratio
    "i_be":         1.0,    # BE trigger in R
    "fvg_min":      0.0005, # FVG minimum size (% of price)
    "mss_window":   30,     # bars after sweep for MSS to fire
    "fvg_expiry":   10,     # bars FVG stays active
}

SYSTEM = """You are a quantitative trading system architect who specializes in ICT methodology strategy optimization for NQ Futures and XAUUSD. You have 20 years optimizing prop firm systems.

You analyze performance data and suggest SPECIFIC parameter changes with exact values.

Rules:
- Only suggest changes backed by clear data patterns
- Never increase risk per trade beyond $500
- Warn if sample < 30 trades
- For each suggestion: state the param, current value, new value, and WHY (specific data point)
- If the strategy is performing well (>55% win rate, positive expectancy), say HOLD — don't over-optimize
- Conservative > aggressive: one change at a time"""


def generate_evolution_report(trades: list[dict], stats: dict, breakdowns: dict) -> str:
    recent_20 = [{
        "date":      t["date"],
        "session":   t["session"],
        "direction": t["direction"],
        "grade":     t.get("grade"),
        "sweep":     t.get("sweep_type"),
        "outcome":   t["outcome"],
        "pnl":       t.get("pnl"),
        "r":         t.get("r_multiple"),
    } for t in trades[:20]]

    prompt = f"""Review NOVA ICT strategy performance and recommend parameter changes:

## Current Parameters
{json.dumps(CURRENT_PARAMS, indent=2)}

## Overall Stats
{json.dumps(stats, indent=2)}

## Pattern Breakdowns
{json.dumps(breakdowns, indent=2)}

## Last 20 Trades
{json.dumps(recent_20, indent=2)}

Recommend specific parameter changes, or say HOLD if the system is performing well."""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def save_evolution_report(stats: dict, analysis: str) -> str:
    now = datetime.now()
    os.makedirs(OBSIDIAN_DIR, exist_ok=True)
    path = os.path.join(OBSIDIAN_DIR, now.strftime("%Y-%m-%d") + "-evolution-report.md")
    content = f"""# NOVA Evolution Report — {now.strftime("%Y-%m-%d")}

## Current Parameters

```json
{json.dumps(CURRENT_PARAMS, indent=2)}
```

## Performance Summary

```json
{json.dumps(stats, indent=2)}
```

## Evolution Recommendations

{analysis}

---
*Apply changes manually to nova_ict_strategy.pine after review*
*nova_evolution_agent.py — {now.strftime("%Y-%m-%d %H:%M")}*
"""
    open(path, "w", encoding="utf-8").write(content)
    return path


def run_evolution(print_output: bool = True) -> dict:
    trades = get_trades(limit=500)
    if not trades:
        return {"error": "No trades in database yet"}

    stats      = get_stats()
    breakdowns = compute_breakdowns([t for t in trades if t["outcome"] != "open"])

    if (stats["total"] or 0) < 10:
        return {"error": f"Only {stats['total']} trades — need at least 10 to evolve"}

    analysis = generate_evolution_report(trades, stats, breakdowns)
    path     = save_evolution_report(stats, analysis)

    if print_output:
        print(f"Trades analyzed: {stats['total']}")
        print(f"Win rate: {stats['win_rate']}%  |  Total P&L: ${stats['total_pnl']:+.0f}  |  Avg R: {stats['avg_r']}")
        print("\n" + "="*60)
        print(analysis)
        print(f"\nReport: {path}")

    return {"stats": stats, "analysis": analysis, "report": path}


if __name__ == "__main__":
    result = run_evolution()
    if "error" in result:
        print(f"[evolution] {result['error']}")
