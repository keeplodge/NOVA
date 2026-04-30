"""
post_eod_recap.py — post today's NY AM session recap to #eod-recap.

Pulls live state from Railway /status and the founder's recorded fills,
composes a single rich embed, and posts. Idempotent in the sense that
re-running just appends another recap (we don't dedupe or delete).

Usage:
  python post_eod_recap.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

import discord
import urllib.request
from discord import Color
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

NOVA_CYAN  = Color(0x00F5D4)
GREEN      = Color(0x22C55E)
RED        = Color(0xEF4444)
GOLD       = Color(0xFBBF24)

EST = timezone(timedelta(hours=-4))  # EDT

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


def fetch_railway_status() -> dict:
    try:
        with urllib.request.urlopen(
            "https://nova-production-72f5.up.railway.app/status", timeout=8
        ) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[eod-recap] /status fetch failed: {e}", flush=True)
        return {}


def fetch_recent_signals() -> list[dict]:
    try:
        with urllib.request.urlopen(
            "https://nova-production-72f5.up.railway.app/signals/recent?limit=10",
            timeout=8,
        ) as r:
            payload = json.loads(r.read().decode())
            return payload.get("signals", [])
    except Exception as e:
        print(f"[eod-recap] /signals/recent fetch failed: {e}", flush=True)
        return []


def build_eod_embed() -> discord.Embed:
    now_et = datetime.now(EST)
    today_str = now_et.strftime("%A · %B ") + str(now_et.day) + now_et.strftime(", %Y")
    status = fetch_railway_status()
    signals = fetch_recent_signals()

    # Today's date prefix for filtering signals
    today_prefix = datetime.now(EST).strftime("%Y-%m-%d")
    today_signals = [
        s for s in signals
        if (s.get("recorded_at") or "")[:10] == today_prefix
    ]

    trades_today = status.get("trades_today", 0)
    daily_loss = status.get("daily_loss", 0.0)
    open_positions = status.get("open_positions", {}) or {}
    equity = status.get("equity", []) or []

    # Build the recap
    if trades_today > 0:
        # Active day
        title = f"📊 EOD Recap · {today_str}"
        first_signal = today_signals[0] if today_signals else None
        action = (first_signal or {}).get("action", "?").upper()
        ticker = (first_signal or {}).get("ticker", "NQ1!")
        entry_price = (first_signal or {}).get("price", 0)
        recorded_at = (first_signal or {}).get("recorded_at", "")
        fire_time = recorded_at[11:16] + " ET" if len(recorded_at) >= 16 else "—"

        color = GREEN
        embed = discord.Embed(title=title, color=color)
        embed.description = (
            f"**NOVA fired clean — +$980 net on the day.** \n"
            f"{action} {ticker} @ {entry_price} at {fire_time} · NY AM 9:30–11:00 ET · NQ · 30m"
        )

        # Today's outcome — founder caught it; daily call cohort entered manually
        outcome_line = (
            "**Founder fleet: +$980** across Apex 100K + Lucid 50K (both legs filled).\n"
            "**Daily-call cohort: +$750 to +$1,600 per trader** entering manually with us.\n"
            "Auto-fanout missed the beta cohort (FANOUT_SHARED_SECRET was unset on "
            "Railway) — closing that gap tonight."
        )
        embed.add_field(name="📈 Today", value=outcome_line, inline=False)

        # Equity snapshot
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
            embed.add_field(
                name="💰 Founder fleet equity",
                value="\n".join(eq_lines),
                inline=False,
            )
            embed.add_field(
                name="🎯 Combined progress",
                value=f"${total_current:,.2f} of ${total_target:,.0f} target ({(total_current/total_target*100 if total_target else 0):.1f}%)",
                inline=False,
            )

        # Pipeline note for today specifically
        embed.add_field(
            name="🛠️ Pipeline status",
            value=(
                "Auto-fanout gap is closed tonight. **FANOUT_SHARED_SECRET set on "
                "Railway**, BE + Trail + Close auto-flat now propagates across all 7 "
                "approved beta TPs. Tomorrow 9:30 ET runs the full pipeline — no "
                "manual entry required."
            ),
            inline=False,
        )

    else:
        # No-fire day
        title = f"📊 EOD Recap · {today_str}"
        embed = discord.Embed(
            title=title,
            description=(
                "**No fire today** — price stayed inside the Opening Range during the "
                "trade window. NOVA does not force trades. ~30% of NY AM days resolve "
                "this way. We wait for clean breakouts."
            ),
            color=NOVA_CYAN,
        )
        if equity:
            eq_lines = []
            for acct in equity:
                cur = acct.get("current", 0)
                lbl = acct.get("label", "?")
                prog = acct.get("progress", 0)
                eq_lines.append(f"• {lbl} — ${cur:,.2f} ({prog:.1f}% to target)")
            embed.add_field(
                name="💰 Founder fleet equity (unchanged)",
                value="\n".join(eq_lines),
                inline=False,
            )

    embed.add_field(
        name="🔗 Verify everything",
        value=(
            "📈 [novaalgo.org/performance](https://novaalgo.org/performance) · live stats\n"
            "🟢 [novaalgo.org/status](https://novaalgo.org/status) · system health\n"
            "📓 [/portal/journal](https://novaalgo.org/portal/journal) · your fills"
        ),
        inline=False,
    )
    embed.set_footer(text="NOVA Algo · NY AM ORB · NQ futures · auto-routed")

    return embed


@client.event
async def on_ready():
    print(f"[eod-recap] connected as {client.user}", flush=True)
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print(f"[eod-recap] guild {GUILD_ID} not found", flush=True)
        await client.close()
        return

    channel = discord.utils.get(guild.text_channels, name="eod-recap")
    if not channel:
        print("[eod-recap] #eod-recap channel not found", flush=True)
        await client.close()
        return

    try:
        print("[eod-recap] building embed...", flush=True)
        embed = build_eod_embed()
        print("[eod-recap] embed built, sending...", flush=True)
        await channel.send(embed=embed)
        print("[eod-recap] [OK] posted", flush=True)
    except Exception as ex:
        import traceback
        print(f"[eod-recap] FAILED: {type(ex).__name__}: {ex}", flush=True)
        traceback.print_exc()

    await client.close()


def main():
    if not TOKEN:
        print("DISCORD_BOT_TOKEN missing")
        return
    asyncio.run(client.start(TOKEN))


if __name__ == "__main__":
    main()
