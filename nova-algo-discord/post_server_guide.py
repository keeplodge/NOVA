"""
post_server_guide.py — post the rich, opinionated Server Guide to #server-guide.

Wipes any previous content in #server-guide first so this is idempotent —
re-running replaces the guide cleanly. Posts 6 embeds in order:

  1. Welcome / what NOVA Algo is
  2. First 5 minutes for new members (checklist)
  3. Daily rhythm (9:30 OR → 10:00 arm → 11:00 flat)
  4. Channel map by category (where to go for what)
  5. Roles & tiers (what each tier unlocks)
  6. Where to ask + house rules

Usage:
  python post_server_guide.py
"""
from __future__ import annotations

import asyncio
import os

import discord
from discord import Color
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

NOVA_CYAN  = Color(0x00F5D4)
GOLD       = Color(0xFBBF24)
GREEN      = Color(0x22C55E)
BLUE       = Color(0x3B82F6)
PURPLE     = Color(0xA855F7)
GREY       = Color(0x6B7280)

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


def build_embeds() -> list[discord.Embed]:
    embeds: list[discord.Embed] = []

    # ── 1. Welcome / what NOVA Algo is ──
    e1 = discord.Embed(
        title="🌌 Welcome to NOVA Algo",
        description=(
            "Auto-routed **NY AM Opening Range Breakout** for NQ futures — "
            "fired from a master TradingView strategy, fanned out to your "
            "TradersPost-connected prop account. Same algo we trade ourselves."
        ),
        color=NOVA_CYAN,
    )
    e1.add_field(
        name="🎯 What NOVA does",
        value=(
            "• **9:30 ET** prints the Opening Range (high + low)\n"
            "• **10:00 ET** arms triggers (long above OR-high, short below OR-low)\n"
            "• First breakout wins — **one trade per day max**\n"
            "• Auto-managed exits: BE at +$500, trailing stop at +$750, force-flat 11:00 ET\n"
            "• **$500 SL / $2000 TP** per contract on NQ ($20/pt)"
        ),
        inline=False,
    )
    e1.add_field(
        name="📊 The track record",
        value=(
            "**322 trades** · **82.6% win rate** · **PF 7.78** · **+$155,645 net**\n"
            "Backtested Jan 1 2025 → Apr 25 2026 on NQ 30m. Live forward-testing "
            "since 2026-04-21 — running totals at <https://novaalgo.org/performance>."
        ),
        inline=False,
    )
    e1.set_footer(text="NOVA Algo · NY AM ORB · NQ futures · auto-routed")
    embeds.append(e1)

    # ── 2. First 5 minutes ──
    e2 = discord.Embed(
        title="⚡ First 5 minutes — new member checklist",
        description="In order. None of these take more than 60 seconds.",
        color=GOLD,
    )
    e2.add_field(
        name="1. Read **#rules**",
        value="The non-negotiables. We keep them short.",
        inline=False,
    )
    e2.add_field(
        name="2. Drop a hello in **#introduce-yourself**",
        value=(
            "Three lines: where you trade from, prop firm + account size, "
            "what you're hoping to get out of NOVA. That's it."
        ),
        inline=False,
    )
    e2.add_field(
        name="3. Connect Discord → portal",
        value=(
            "Visit <https://novaalgo.org/portal> while signed in and click "
            "**Connect Discord**. This unlocks your tier role automatically."
        ),
        inline=False,
    )
    e2.add_field(
        name="4. Wire your TradersPost webhook",
        value=(
            "Walk through **#onboarding-tradespost**. NOVA can't route trades to "
            "your account until your webhook URL is on file."
        ),
        inline=False,
    )
    e2.add_field(
        name="5. Wait for **9:30 ET tomorrow**",
        value=(
            "That's it. The next NY AM session, you'll see NOVA fire in "
            "**#live-signals** and (if approved) on your prop account."
        ),
        inline=False,
    )
    embeds.append(e2)

    # ── 3. Daily rhythm ──
    e3 = discord.Embed(
        title="⏱️ Daily rhythm — what to expect",
        description="All times America/New_York. NQ futures trade 24/5; we only fire NY AM.",
        color=BLUE,
    )
    e3.add_field(
        name="9:30 — OR bar prints",
        value="The first 30-minute candle defines the Opening Range high + low.",
        inline=False,
    )
    e3.add_field(
        name="10:00 — triggers armed",
        value=(
            "Stop orders armed at OR-high+1 tick (long) and OR-low-1 tick (short). "
            "First direction to break wins. **One trade per day max.**"
        ),
        inline=False,
    )
    e3.add_field(
        name="10:00–11:00 — trade window",
        value=(
            "If price breaks OR, NOVA fires. Bracket attaches: $500 SL / $2000 TP. "
            "BE moves to entry once you're +$500 in profit. Trail activates at +$750."
        ),
        inline=False,
    )
    e3.add_field(
        name="11:00 — session flat",
        value="Any open position auto-closes at the open. We don't carry NY AM positions into NY PM.",
        inline=False,
    )
    e3.add_field(
        name="No fire days (~30%)",
        value=(
            "If price stays inside the OR for the whole 10:00–10:30 window, no trade. "
            "That's normal — not every day breaks. We don't force trades."
        ),
        inline=False,
    )
    embeds.append(e3)

    # ── 4. Channel map ──
    e4 = discord.Embed(
        title="🗺️ Channel map — where to go for what",
        description="Skim these. You'll know exactly where to land for any question.",
        color=NOVA_CYAN,
    )
    e4.add_field(
        name="🌌 START HERE",
        value=(
            "📢 **#welcome** — what NOVA is\n"
            "📢 **#rules** — non-negotiables\n"
            "📢 **#server-guide** — this channel\n"
            "💬 **#introduce-yourself** — drop a hello\n"
            "📢 **#onboarding-tradespost** — wire your webhook"
        ),
        inline=False,
    )
    e4.add_field(
        name="📡 LIVE SIGNALS",
        value=(
            "📢 **#live-signals** — every NOVA fire posted in real time\n"
            "📢 **#equity-curve** — daily founder fleet snapshot\n"
            "📢 **#halt-events** — if routing pauses, you hear it here\n"
            "📢 **#fanout-failures** — config issues surface in this channel"
        ),
        inline=False,
    )
    e4.add_field(
        name="📈 STRATEGY & ANALYSIS",
        value=(
            "💬 **#trade-talk** — discuss today's fire, yesterday's tape\n"
            "💬 **#chart-share** — drop screenshots, get reads\n"
            "📢 **#morning-brief** — daily pre-market posted by NOVA\n"
            "📢 **#eod-recap** — daily wrap"
        ),
        inline=False,
    )
    e4.add_field(
        name="🛠️ HELP & SUPPORT",
        value=(
            "💬 **#help** — questions about NOVA, the algo, your portal\n"
            "💬 **#open-ticket** — anything that needs founder/co-founder eyes\n"
            "💬 **#feedback** — what's working, what isn't"
        ),
        inline=False,
    )
    e4.add_field(
        name="🎙️ COMMUNITY",
        value=(
            "💬 **#general** — open chat\n"
            "💬 **#wins** — share your green days\n"
            "💬 **#off-topic** — non-trading conversation\n"
            "🔊 **Daily call** — voice room during NY AM session"
        ),
        inline=False,
    )
    e4.set_footer(text="🔒 = staff-only · ⭐ = tier-locked · 📢 = read-only · 💬 = open chat")
    embeds.append(e4)

    # ── 5. Tiers ──
    e5 = discord.Embed(
        title="🎟️ Tiers & roles — what each unlocks",
        description="Every member gets a tier role. Higher tiers unlock more channels + features.",
        color=GOLD,
    )
    e5.add_field(
        name="🟢 Beta · current cohort · *Fleet free forever*",
        value=(
            "First 11 members. **You're in this group.** Once paid tiers go live, "
            "every Beta member keeps **Fleet permanently** — no upgrade prompt, no "
            "expiration. You're locked in."
        ),
        inline=False,
    )
    e5.add_field(
        name="🔵 Signal · $97/mo (when launched)",
        value=(
            "NOVA signals delivered to your phone. You execute manually on your own broker. "
            "No auto-routing."
        ),
        inline=False,
    )
    e5.add_field(
        name="🟣 Auto · $297/mo (when launched)",
        value=(
            "Auto-routed via TradersPost. NOVA fires → your prop account executes "
            "automatically. Set it and forget it."
        ),
        inline=False,
    )
    e5.add_field(
        name="🟡 Fleet · $997/mo (when launched)",
        value=(
            "Auto-routing across **up to 5 prop accounts** simultaneously. Best for "
            "traders running multiple evals. **Free forever for current Beta members.**"
        ),
        inline=False,
    )
    e5.add_field(
        name="🛡️ Founder + Co-Founder roles",
        value=(
            "Server staff. **Gee** runs strategy + product. **John** runs community + Discord ops. "
            "**Victor** runs marketing. Tag any of them in **#open-ticket** if needed."
        ),
        inline=False,
    )
    embeds.append(e5)

    # ── 6. House rules + where to ask ──
    e6 = discord.Embed(
        title="🤝 House rules + where to ask",
        description="Short list. We don't moderate to a 50-page rulebook.",
        color=GREEN,
    )
    e6.add_field(
        name="📜 The 5 non-negotiables",
        value=(
            "**1.** No financial advice or signal-giving outside **#trade-talk**\n"
            "**2.** No selling other tools / referrals without staff approval\n"
            "**3.** No revealing other members' webhook URLs, P&L, or account details\n"
            "**4.** Stay on-topic per channel\n"
            "**5.** No politics, no NSFW, no harassment — instant kick"
        ),
        inline=False,
    )
    e6.add_field(
        name="❓ Where to ask",
        value=(
            "• **General Q's about NOVA** → **#help**\n"
            "• **Portal / TradersPost setup** → **#onboarding-tradespost** then **#help**\n"
            "• **Anything that needs founder eyes** → **#open-ticket**\n"
            "• **Feedback on the product** → **#feedback**\n"
            "• **Just want to chat** → **#general** or **#off-topic**"
        ),
        inline=False,
    )
    e6.add_field(
        name="🔗 Key links",
        value=(
            "🌐 [novaalgo.org](https://novaalgo.org) — site\n"
            "🪪 [/portal](https://novaalgo.org/portal) — your dashboard\n"
            "🔌 [/portal/connect](https://novaalgo.org/portal/connect) — wire TradersPost\n"
            "📓 [/portal/journal](https://novaalgo.org/portal/journal) — your trade journal\n"
            "📊 [/performance](https://novaalgo.org/performance) — live verified stats\n"
            "🟢 [/status](https://novaalgo.org/status) — system health"
        ),
        inline=False,
    )
    e6.set_footer(text="That's it. Welcome aboard. — Gee")
    embeds.append(e6)

    return embeds


@client.event
async def on_ready():
    print(f"[server-guide] connected as {client.user}")
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print(f"[server-guide] guild {GUILD_ID} not found")
        await client.close()
        return

    channel = discord.utils.get(guild.text_channels, name="server-guide")
    if not channel:
        print("[server-guide] #server-guide channel not found — run setup.py first")
        await client.close()
        return

    # Wipe old messages so we re-post cleanly
    deleted = 0
    try:
        async for msg in channel.history(limit=50):
            try:
                await msg.delete()
                deleted += 1
            except Exception as e:
                print(f"[server-guide] couldn't delete msg {msg.id}: {e}")
    except Exception as e:
        print(f"[server-guide] history fetch failed: {e}")
    if deleted:
        print(f"[server-guide] cleared {deleted} prior messages")

    embeds = build_embeds()
    print(f"[server-guide] sending {len(embeds)} embeds...", flush=True)
    sent_ok = 0
    for i, e in enumerate(embeds, 1):
        try:
            await channel.send(embed=e)
            sent_ok += 1
            print(f"[server-guide]   [OK] embed {i}/{len(embeds)} sent", flush=True)
        except Exception as ex:
            import traceback
            print(f"[server-guide]   [FAIL] embed {i} FAILED: {type(ex).__name__}: {ex}", flush=True)
            traceback.print_exc()
    print(f"[server-guide] [OK] posted {sent_ok}/{len(embeds)} embeds", flush=True)

    await client.close()


@client.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"[server-guide] EVENT ERROR in {event}:", flush=True)
    traceback.print_exc()


def main():
    if not TOKEN:
        print("DISCORD_BOT_TOKEN missing")
        return
    asyncio.run(client.start(TOKEN))


if __name__ == "__main__":
    main()
