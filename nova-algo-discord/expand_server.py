"""
expand_server.py — Wave 1 server expansion.

Adds:
  • 4 tier-locked voice channels (Beta/Signal/Auto/Fleet) inside TIER LOUNGES
    — each gated to its tier and above, just like the matching text channels.
  • Town Hall voice channel under ANNOUNCEMENTS — public listen, members only
    can speak when explicitly granted.
  • Audits every existing channel's category placement and reports drift.
  • Configures AutoMod: slur filter, link/invite filter, mention spam.

Idempotent: existing channels/rules by name are left alone.
"""
from __future__ import annotations

import asyncio
import os

import discord
from discord import PermissionOverwrite
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

# Additional prop firm flair roles. The original setup had Apex/TopStep/Lucid;
# this expands to the major NQ-futures-focused firms members are likely on.
NEW_PROP_FIRM_ROLES = [
    "Tradeify",
    "Take Profit Trader",
    "TopOne Trader",
    "MyFundedFutures",
    "Earn2Trade",
    "Bulenox",
    "The Trading Pit",
    "FundedNext",
    "Aqua Funded",
]

# Tier voice rooms — each one allowed for that tier AND every higher tier.
TIER_VOICE_CHANNELS = [
    # (channel_name, allowed_tier_roles)
    ("Beta Voice",   ["Beta", "Signal", "Auto", "Fleet"]),
    ("Signal Voice", ["Signal", "Auto", "Fleet"]),
    ("Auto Voice",   ["Auto", "Fleet"]),
    ("Fleet Voice",  ["Fleet"]),
]
TIER_LOUNGE_CATEGORY = "🏆 TIER LOUNGES"

TOWN_HALL_NAME = "🏛 Town Hall"
TOWN_HALL_CATEGORY = "📣 ANNOUNCEMENTS"

# Expected category for each existing channel — used by the audit pass.
EXPECTED_CATEGORY = {
    # START HERE
    "rules": "🌌 START HERE", "welcome": "🌌 START HERE", "server-guide": "🌌 START HERE",
    "introduce-yourself": "🌌 START HERE", "verify": "🌌 START HERE",
    "onboarding-tradespost": "🌌 START HERE",
    # ANNOUNCEMENTS
    "announcements": "📣 ANNOUNCEMENTS", "changelog": "📣 ANNOUNCEMENTS",
    "roadmap": "📣 ANNOUNCEMENTS", "status": "📣 ANNOUNCEMENTS",
    "milestones": "📣 ANNOUNCEMENTS",
    # LIVE TRADING
    "live-signals": "📊 LIVE TRADING", "morning-brief": "📊 LIVE TRADING",
    "pre-market": "📊 LIVE TRADING", "session-open": "📊 LIVE TRADING",
    "eod-recap": "📊 LIVE TRADING", "trade-journal": "📊 LIVE TRADING",
    "equity-curve": "📊 LIVE TRADING", "key-levels": "📊 LIVE TRADING",
    "news-feed": "📊 LIVE TRADING",
    # EDUCATION
    "ict-fundamentals": "🎓 EDUCATION", "strategy-deep-dive": "🎓 EDUCATION",
    "video-lessons": "🎓 EDUCATION", "market-structure": "🎓 EDUCATION",
    "concept-of-the-week": "🎓 EDUCATION", "resource-vault": "🎓 EDUCATION",
    "ask-a-coach": "🎓 EDUCATION",
    # COMMUNITY
    "general": "💬 COMMUNITY", "wins": "💬 COMMUNITY", "screenshots": "💬 COMMUNITY",
    "strategy-talk": "💬 COMMUNITY", "off-topic": "💬 COMMUNITY",
    "coffee-chat": "💬 COMMUNITY",
    # TIER LOUNGES
    "beta-lounge": "🏆 TIER LOUNGES", "signal-lounge": "🏆 TIER LOUNGES",
    "auto-lounge": "🏆 TIER LOUNGES", "fleet-lounge": "🏆 TIER LOUNGES",
    "fleet-vault": "🏆 TIER LOUNGES",
    # TOOLS
    "bot-commands": "🛠 TOOLS & UTILITIES", "position-sizer": "🛠 TOOLS & UTILITIES",
    "economic-calendar": "🛠 TOOLS & UTILITIES",
    "stats-dashboard": "🛠 TOOLS & UTILITIES",
    "backtest-results": "🛠 TOOLS & UTILITIES",
    # VOICE (public)
    "Live Trading Floor": "🎙 VOICE", "Strategy Talk": "🎙 VOICE",
    "Education Hall": "🎙 VOICE", "Lounge": "🎙 VOICE",
    "AMA Voice": "🎙 VOICE",
    # SUPPORT
    "open-ticket": "🎫 SUPPORT", "bug-reports": "🎫 SUPPORT",
    "suggestions": "🎫 SUPPORT", "feedback": "🎫 SUPPORT",
    "contact-founder": "🎫 SUPPORT",
    # STAFF
    "founder-notes": "🔒 STAFF", "bot-logs": "🔒 STAFF",
    "signal-audit": "🔒 STAFF", "halt-events": "🔒 STAFF",
    "fanout-failures": "🔒 STAFF", "partnership-inbox": "🔒 STAFF",
}

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


def _tier_voice_overwrites(guild, allowed_tier_names: list[str]) -> dict:
    everyone = guild.default_role
    bot = guild.me
    role_lookup = {r.name: r for r in guild.roles}

    ow: dict = {everyone: PermissionOverwrite(view_channel=False, connect=False, speak=False)}
    for name in allowed_tier_names:
        r = role_lookup.get(name)
        if r:
            ow[r] = PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True,
                use_voice_activation=True,
            )
    for staff in ("Founder", "Co-Founder", "Moderator", "Coach"):
        r = role_lookup.get(staff)
        if r:
            ow[r] = PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True,
                priority_speaker=True, mute_members=True, deafen_members=True,
                move_members=True,
            )
    ow[bot] = PermissionOverwrite(view_channel=True, connect=True, speak=True)
    return ow


async def add_tier_voice_channels(guild):
    cat = discord.utils.get(guild.categories, name=TIER_LOUNGE_CATEGORY)
    if not cat:
        print(f"  ✗ category {TIER_LOUNGE_CATEGORY!r} not found — skipping tier voice")
        return
    existing = {c.name for c in guild.channels if c.category and c.category.id == cat.id}
    for name, allowed in TIER_VOICE_CHANNELS:
        if name in existing:
            print(f"  · {name} (exists)")
            continue
        ow = _tier_voice_overwrites(guild, allowed)
        try:
            await guild.create_voice_channel(
                name=name, category=cat, overwrites=ow,
                reason="NOVA Algo — tier voice rooms",
            )
            print(f"  ✓ {name} (tiers: {', '.join(allowed)})")
        except discord.HTTPException as e:
            print(f"  ✗ {name} failed: {e}")
        await asyncio.sleep(0.4)


async def add_town_hall(guild):
    cat = discord.utils.get(guild.categories, name=TOWN_HALL_CATEGORY)
    if not cat:
        print(f"  ✗ category {TOWN_HALL_CATEGORY!r} not found — skipping town hall")
        return
    existing = discord.utils.get(guild.voice_channels, name=TOWN_HALL_NAME)
    if existing:
        print(f"  · {TOWN_HALL_NAME} (exists)")
        return

    everyone = guild.default_role
    role_lookup = {r.name: r for r in guild.roles}
    ow: dict = {
        # Everyone can listen by default; speaking is gated to staff. Founder
        # can grant temp speak when running an actual town hall.
        everyone: PermissionOverwrite(view_channel=True, connect=True, speak=False),
    }
    for staff in ("Founder", "Co-Founder", "Moderator", "Coach"):
        r = role_lookup.get(staff)
        if r:
            ow[r] = PermissionOverwrite(
                view_channel=True, connect=True, speak=True, priority_speaker=True,
                mute_members=True, move_members=True,
            )
    ow[guild.me] = PermissionOverwrite(view_channel=True, connect=True, speak=True)

    try:
        await guild.create_voice_channel(
            name=TOWN_HALL_NAME, category=cat, overwrites=ow,
            reason="NOVA Algo — town hall meetings",
        )
        print(f"  ✓ {TOWN_HALL_NAME} (listen-only by default; staff promote speakers per event)")
    except discord.HTTPException as e:
        print(f"  ✗ {TOWN_HALL_NAME} failed: {e}")


async def audit_categories(guild):
    drift_count = 0
    fixed_count = 0
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        expected_cat_name = EXPECTED_CATEGORY.get(ch.name)
        if not expected_cat_name:
            continue  # not in our expected map (Town Hall, tier voices, etc.)
        actual_cat_name = ch.category.name if ch.category else None
        if actual_cat_name == expected_cat_name:
            continue
        drift_count += 1
        new_cat = discord.utils.get(guild.categories, name=expected_cat_name)
        if not new_cat:
            print(f"  ✗ #{ch.name} expected {expected_cat_name!r} but category missing")
            continue
        try:
            await ch.edit(category=new_cat, reason="NOVA Algo audit — moving to expected category")
            print(f"  ✓ moved #{ch.name}: {actual_cat_name!r} → {expected_cat_name!r}")
            fixed_count += 1
            await asyncio.sleep(0.3)
        except discord.HTTPException as e:
            print(f"  ✗ #{ch.name} move failed: {e}")
    if drift_count == 0:
        print("  · no drift detected — all channels in expected categories")
    else:
        print(f"  → {drift_count} drift, {fixed_count} fixed")


async def setup_automod(guild):
    """Configure Discord's built-in AutoMod with sensible defaults.

    Three rules:
      1. Slur filter — Discord's built-in keyword preset
      2. Mention spam — block messages with >5 user mentions
      3. Server invite filter — block discord.gg/* outside trusted channels
    """
    try:
        existing = await guild.fetch_automod_rules()
    except discord.HTTPException as e:
        print(f"  ✗ list automod rules failed: {e}")
        return
    existing_names = {r.name for r in existing}

    # Rule 1 — keyword filter (builtin slurs/profanity preset)
    if "NOVA — slur filter" not in existing_names:
        try:
            await guild.create_automod_rule(
                name="NOVA — slur filter",
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword_preset,
                    presets=[
                        discord.AutoModRulePresetType.profanity,
                        discord.AutoModRulePresetType.sexual_content,
                        discord.AutoModRulePresetType.slurs,
                    ],
                ),
                actions=[discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)],
                enabled=True,
                reason="NOVA Algo — community safety",
            )
            print("  ✓ NOVA — slur filter")
        except discord.HTTPException as e:
            print(f"  ✗ slur filter failed: {e}")
        await asyncio.sleep(0.4)
    else:
        print("  · NOVA — slur filter (exists)")

    # Rule 2 — mention spam
    if "NOVA — mention spam" not in existing_names:
        try:
            await guild.create_automod_rule(
                name="NOVA — mention spam",
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.mention_spam,
                    mention_limit=5,
                ),
                actions=[discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)],
                enabled=True,
                reason="NOVA Algo — anti-mass-mention",
            )
            print("  ✓ NOVA — mention spam")
        except discord.HTTPException as e:
            print(f"  ✗ mention spam failed: {e}")
        await asyncio.sleep(0.4)
    else:
        print("  · NOVA — mention spam (exists)")

    # Rule 3 — invite filter
    if "NOVA — invite filter" not in existing_names:
        try:
            await guild.create_automod_rule(
                name="NOVA — invite filter",
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword,
                    keyword_filter=[
                        "discord.gg/*",
                        "discord.com/invite/*",
                        "discordapp.com/invite/*",
                    ],
                ),
                actions=[discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)],
                enabled=True,
                reason="NOVA Algo — block external server promotion",
            )
            print("  ✓ NOVA — invite filter")
        except discord.HTTPException as e:
            print(f"  ✗ invite filter failed: {e}")
    else:
        print("  · NOVA — invite filter (exists)")


async def add_prop_firm_roles(guild):
    existing = {r.name: r for r in guild.roles}
    grey = discord.Color(0x6B7280)
    for name in NEW_PROP_FIRM_ROLES:
        if name in existing:
            print(f"  · {name} (exists)")
            continue
        try:
            await guild.create_role(
                name=name, color=grey, hoist=False, mentionable=True,
                reason="NOVA Algo — prop firm flair",
            )
            print(f"  ✓ {name}")
        except discord.HTTPException as e:
            print(f"  ✗ {name} failed: {e}")
        await asyncio.sleep(0.3)


@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print("guild not found"); await client.close(); return
    print(f"Connected to {guild.name}\n")

    print("[1/5] Adding tier voice channels...")
    await add_tier_voice_channels(guild)

    print("\n[2/5] Adding Town Hall...")
    await add_town_hall(guild)

    print("\n[3/5] Adding prop firm flair roles...")
    await add_prop_firm_roles(guild)

    print("\n[4/5] Auditing channel categories...")
    await audit_categories(guild)

    print("\n[5/5] Configuring AutoMod rules...")
    await setup_automod(guild)

    print("\n✓ done.")
    await client.close()


client.run(TOKEN, log_handler=None)
