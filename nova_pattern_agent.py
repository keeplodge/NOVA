"""
nova_pattern_agent.py — Pattern recognition from winning trades.

Scans all historical trades, identifies the optimal setup fingerprint,
and flags what to look for on the next trade.
"""
import os, json
from datetime import datetime
from collections import defaultdict
from anthropic import Anthropic
from nova_trade_db import get_trades, get_stats

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OBSIDIAN_DIR      = r"C:\Users\User\nova\nova-brain\04_Strategy"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = """You are a quantitative ICT strategy analyst. You study trade databases to find the exact combination of factors that produces the highest win rate.

You're looking for the NOVA winning fingerprint — the session + sweep type + grade + direction combination that wins most. You also identify what to AVOID.

Rules:
- If sample < 20 trades, flag it but still analyze what's there
- Be specific: name exact sessions, exact sweep types, exact grades
- Give one concrete filter that would improve win rate immediately
- Don't hedge — give a definitive recommendation"""


def compute_breakdowns(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["outcome"] in ("win", "loss", "be")]
    if not closed:
        return {}

    def wr(subset):
        wins = sum(1 for t in subset if t["outcome"] == "win")
        return round(wins / len(subset) * 100, 1) if subset else 0.0

    by_session   = defaultdict(list)
    by_grade     = defaultdict(list)
    by_sweep     = defaultdict(list)
    by_direction = defaultdict(list)
    by_sess_dir  = defaultdict(list)

    for t in closed:
        by_session[t["session"]].append(t)
        by_grade[t.get("grade") or "Unknown"].append(t)
        by_sweep[t.get("sweep_type") or "Unknown"].append(t)
        by_direction[t["direction"]].append(t)
        by_sess_dir[f"{t['session']}_{t['direction']}"].append(t)

    return {
        "total_closed":   len(closed),
        "overall_wr":     wr(closed),
        "total_pnl":      round(sum(t.get("pnl") or 0 for t in closed), 2),
        "avg_r":          round(sum(t.get("r_multiple") or 0 for t in closed) / len(closed), 2),
        "by_session":     {s: {"count": len(ts), "win_rate": wr(ts)} for s, ts in by_session.items()},
        "by_grade":       {g: {"count": len(ts), "win_rate": wr(ts)} for g, ts in by_grade.items()},
        "by_sweep":       {s: {"count": len(ts), "win_rate": wr(ts)} for s, ts in by_sweep.items()},
        "by_direction":   {d: {"count": len(ts), "win_rate": wr(ts)} for d, ts in by_direction.items()},
        "by_sess_dir":    {k: {"count": len(ts), "win_rate": wr(ts)} for k, ts in by_sess_dir.items()},
    }


def generate_pattern_report(breakdowns: dict) -> str:
    prompt = f"""Analyze this NOVA ICT strategy trade data and identify the winning setup fingerprint:

{json.dumps(breakdowns, indent=2)}

Provide:
1. The optimal setup (session + sweep type + grade + direction — the exact combo with the highest win rate)
2. The worst combo to avoid
3. One filter to add immediately to cut losing trades
4. Overall verdict"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def save_to_obsidian(breakdowns: dict, analysis: str) -> str:
    now = datetime.now()
    os.makedirs(OBSIDIAN_DIR, exist_ok=True)
    path = os.path.join(OBSIDIAN_DIR, now.strftime("%Y-%m-%d") + "-pattern-report.md")
    content = f"""# NOVA Pattern Report — {now.strftime("%Y-%m-%d")}

## Trade Breakdown

```json
{json.dumps(breakdowns, indent=2)}
```

## Winning Fingerprint Analysis

{analysis}

---
*nova_pattern_agent.py — {now.strftime("%Y-%m-%d %H:%M")}*
"""
    open(path, "w", encoding="utf-8").write(content)
    return path


def run_pattern_analysis(print_output: bool = True) -> dict:
    trades = get_trades(limit=500)
    if not trades:
        return {"error": "No trades in database yet"}

    breakdowns = compute_breakdowns(trades)
    if not breakdowns:
        return {"error": "No closed trades to analyze yet"}

    analysis = generate_pattern_report(breakdowns)
    path     = save_to_obsidian(breakdowns, analysis)

    if print_output:
        print(json.dumps(breakdowns, indent=2))
        print("\n" + "="*60)
        print(analysis)
        print(f"\nReport: {path}")

    return {"breakdowns": breakdowns, "analysis": analysis, "report": path}


if __name__ == "__main__":
    result = run_pattern_analysis()
    if "error" in result:
        print(f"[pattern] {result['error']}")
