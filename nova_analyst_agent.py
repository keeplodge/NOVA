"""
nova_analyst_agent.py — Post-trade AI analysis using Claude.

After each trade closes, analyzes setup quality, outcome reasoning,
and what to look for in the next similar setup. Saves to Obsidian + DB.
"""
import os, glob, json
from datetime import datetime
from zoneinfo import ZoneInfo
from anthropic import Anthropic
from nova_trade_db import get_last_open_trade, close_trade, get_stats

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
OBSIDIAN_TRADE_DIR = r"C:\Users\User\nova\nova-brain\01_Trade_Logs"
EST = ZoneInfo("America/New_York")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM = """You are a former prop trading desk manager and ICT methodology expert with 20 years on NQ Futures. You run post-trade analysis for NOVA — an automated ICT strategy trading London and NY AM sessions.

Your job is brutally honest, specific, and actionable. You reference exact ICT concepts: liquidity sweeps (PDH/PDL/Asian Range/Swing), MSS (Market Structure Shift), FVG (Fair Value Gap), killzones.

For every trade:
1. Grade the setup independently — would YOU have taken it? Why?
2. Identify the single biggest factor in the outcome (win or loss)
3. State one specific pattern to look for to repeat this if it won, or avoid it if it lost
4. Flag any grade inflation — if the bot graded it A+ but it was a C setup, say so

Max 160 words. Be the voice of a $10M account manager, not a textbook."""


def analyze_trade(trade: dict) -> str:
    sl  = f"{trade['sl_price']:.2f}"  if trade.get("sl_price")  else "TBD"
    tp  = f"{trade['tp_price']:.2f}"  if trade.get("tp_price")  else "TBD"
    pnl = f"${trade.get('pnl', 0):+.0f}"

    prompt = f"""Post-trade analysis:

Ticker: {trade['ticker']}  |  Direction: {trade['direction'].upper()}  |  Session: {trade['session']}
Entry: {trade['entry_price']}  |  SL: {sl}  |  TP: {tp}  |  Exit: {trade.get('exit_price', '?')}
Outcome: {trade['outcome'].upper()}  |  P&L: {pnl}  |  R: {trade.get('r_multiple', '?')}R
Grade: {trade.get('grade', '?')} ({trade.get('grade_score', '?')}/10)  |  Sweep: {trade.get('sweep_type', 'Unknown')}

Analyze this trade."""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def append_analysis_to_obsidian(trade: dict, analysis: str):
    pattern = os.path.join(
        OBSIDIAN_TRADE_DIR,
        f"{trade['date']}*{trade['session'].lower().replace('_','')}*{trade['direction']}*.md"
    )
    files = glob.glob(pattern)
    if not files:
        files = sorted(glob.glob(os.path.join(OBSIDIAN_TRADE_DIR, f"{trade['date']}*.md")), reverse=True)
    if not files:
        return

    path = files[0]
    try:
        content = open(path, encoding="utf-8").read()
        if "## NOVA Analysis" in content:
            return
        section = f"""

---

## NOVA Analysis

{analysis}

**Result:** {trade['outcome'].upper()} | **P&L:** ${trade.get('pnl', 0):+.0f} | **R:** {trade.get('r_multiple', 0):+.1f}R
"""
        content += section
        open(path, "w", encoding="utf-8").write(content)
    except Exception as e:
        print(f"[analyst] Obsidian update failed: {e}")


def run_post_trade_analysis(outcome: str, exit_price: float) -> dict:
    """
    Call after a trade closes. Finds the open trade, runs Claude analysis,
    closes it in the DB, and appends analysis to the Obsidian log.
    """
    trade = get_last_open_trade()
    if not trade:
        return {"error": "No open trade found in database"}

    trade["outcome"]    = outcome
    trade["exit_price"] = exit_price
    pnl_map = {"win": 1000.0, "loss": -500.0, "be": 0.0}
    trade["pnl"] = pnl_map.get(outcome, 0.0)

    print(f"[analyst] Analyzing trade #{trade['id']} — {outcome.upper()} @ {exit_price}")
    analysis = analyze_trade(trade)
    close_trade(trade["id"], outcome, exit_price, analysis)
    append_analysis_to_obsidian(trade, analysis)

    stats = get_stats()
    return {
        "trade_id": trade["id"],
        "outcome":  outcome,
        "analysis": analysis,
        "stats":    stats,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python nova_analyst_agent.py <win|loss|be> <exit_price>")
        sys.exit(1)

    outcome    = sys.argv[1].lower()
    exit_price = float(sys.argv[2])
    result     = run_post_trade_analysis(outcome, exit_price)

    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(f"\nTrade #{result['trade_id']} closed — {result['outcome'].upper()}")
        print("\n" + "="*60)
        print(result["analysis"])
        print("="*60)
        print(f"\nOverall stats: {json.dumps(result['stats'], indent=2)}")
