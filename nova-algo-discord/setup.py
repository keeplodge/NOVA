"""
NOVA Algo Discord — one-shot server provisioner.

Provisions categories, channels, roles, permissions, server icon/banner,
welcome content, and webhooks for Railway → Discord signal piping.

Idempotent: safe to re-run. Existing categories/channels/roles by name
are kept as-is; only missing pieces are added.

Usage:
  pip install -r requirements.txt
  python setup.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import discord
from discord import Color, PermissionOverwrite
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or "0")
APP_ID = os.environ.get("DISCORD_APP_ID", "")

NOVA_CYAN = Color(0x00F5D4)
GOLD = Color(0xFBBF24)
SILVER = Color(0xC0C0C0)
RED = Color(0xEF4444)
PURPLE = Color(0xA855F7)
ORANGE = Color(0xF97316)
BLUE = Color(0x3B82F6)
MINT = Color(0x6EE7B7)
GREEN = Color(0x22C55E)
TEAL = Color(0x14B8A6)
EMERALD = Color(0x10B981)
GREY = Color(0x6B7280)

# (name, color, hoist, mentionable) — TOP→BOTTOM hierarchy
ROLES = [
    ("Founder", GOLD, True, True),
    ("Co-Founder", SILVER, True, True),
    ("Marketing", Color(0xEC4899), True, True),
    ("Moderator", RED, True, True),
    ("Coach", PURPLE, True, True),
    ("Fleet", NOVA_CYAN, True, True),
    ("Auto", ORANGE, True, True),
    ("Signal", BLUE, True, True),
    ("Beta", MINT, True, True),
    ("Verified", GREEN, False, False),
    ("TradersPost Connected", TEAL, False, False),
    ("Funded", EMERALD, False, False),
    ("Apex", GREY, False, True),
    ("TopStep", GREY, False, True),
    ("Lucid", GREY, False, True),
    ("Member", Color.default(), False, False),
]

# Channel spec: (name, type, perm_pattern, topic, slowmode_seconds)
# perm_pattern: "open" | "read_only" | "tier:Beta|Fleet" | "staff"
CATEGORIES = [
    ("🌌 START HERE", [
        ("rules", "text", "read_only", "Server rules. Read before posting.", 0),
        ("welcome", "text", "read_only", "Welcome to NOVA Algo.", 0),
        ("server-guide", "text", "read_only", "Channel map — what every channel does.", 0),
        ("introduce-yourself", "text", "open", "New here? Drop a hello.", 30),
        ("verify", "text", "open", "Pick your role.", 0),
        ("onboarding-tradespost", "text", "read_only", "Step-by-step TradersPost webhook setup.", 0),
    ]),
    ("📣 ANNOUNCEMENTS", [
        ("announcements", "text", "read_only", "Official NOVA Algo news.", 0),
        ("changelog", "text", "read_only", "Every code, strategy, and infra update.", 0),
        ("roadmap", "text", "read_only", "What's shipping next.", 0),
        ("status", "text", "read_only", "Railway uptime, halt events, incidents.", 0),
        ("milestones", "text", "read_only", "Account flips, beta seat fills, big wins.", 0),
    ]),
    ("📊 LIVE TRADING", [
        ("live-signals", "text", "read_only", "Every NOVA Algo trade fire, live from Railway.", 0),
        ("morning-brief", "text", "read_only", "8:00am ET — NY AM levels, bias, conditions.", 0),
        ("pre-market", "text", "read_only", "Key levels, news, prep before the bell.", 0),
        ("session-open", "text", "read_only", "8:30am ET kickoff, range tracking.", 0),
        ("eod-recap", "text", "read_only", "End-of-day P&L, replay, observations.", 0),
        ("trade-journal", "text", "read_only", "Auto-posted post-mortems for every fired trade.", 0),
        ("equity-curve", "text", "read_only", "Daily Railway equity snapshots per account.", 0),
        ("key-levels", "text", "read_only", "PDH/PDL, weekly opens, session levels.", 0),
        ("news-feed", "text", "read_only", "High-impact macro events.", 0),
    ]),
    ("🎓 EDUCATION", [
        ("ict-fundamentals", "text", "open", "NY AM ORB fundamentals · NOVA does NOT use ICT.", 60),
        ("strategy-deep-dive", "text", "open", "How NOVA's logic actually works.", 60),
        ("video-lessons", "text", "read_only", "Curated video lessons.", 0),
        ("market-structure", "text", "open", "BOS, MSS, liquidity.", 60),
        ("concept-of-the-week", "text", "open", "One concept per week, deep.", 60),
        ("resource-vault", "text", "read_only", "PDFs, charts, cheat sheets.", 0),
        ("ask-a-coach", "text", "open", "Q&A — one thread per question.", 0),
    ]),
    ("💬 COMMUNITY", [
        ("general", "text", "open", "Main chat. Be cool.", 5),
        ("wins", "text", "open", "Screenshots and stories of wins.", 0),
        ("screenshots", "text", "open", "Charts, trades, anything visual.", 0),
        ("strategy-talk", "text", "open", "Trading discussion, market talk.", 30),
        ("off-topic", "text", "open", "Not trading. Anything else.", 30),
        ("coffee-chat", "text", "open", "Mornings, hellos, light chat.", 0),
    ]),
    ("🏆 TIER LOUNGES", [
        ("beta-lounge", "text", "tier:Beta|Fleet|Auto|Signal", "Beta tester lounge.", 0),
        ("signal-lounge", "text", "tier:Signal|Auto|Fleet", "Signal tier lounge.", 0),
        ("auto-lounge", "text", "tier:Auto|Fleet", "Auto tier lounge.", 0),
        ("fleet-lounge", "text", "tier:Fleet", "Fleet tier — the inner circle.", 0),
        ("fleet-vault", "text", "tier:Fleet", "Direct line to the founder. Fleet only.", 0),
    ]),
    ("🛠 TOOLS & UTILITIES", [
        ("bot-commands", "text", "open", "Bot command playground.", 0),
        ("position-sizer", "text", "open", "Risk and position sizing tools.", 0),
        ("economic-calendar", "text", "read_only", "FOMC/NFP/CPI events.", 0),
        ("stats-dashboard", "text", "read_only", "Live equity, win rate, R per session.", 0),
        ("backtest-results", "text", "read_only", "Historical backtest output.", 0),
    ]),
    ("🎙 VOICE", [
        ("Live Trading Floor", "voice", "open", None, 0),
        ("Strategy Talk", "voice", "open", None, 0),
        ("Education Hall", "voice", "open", None, 0),
        ("Lounge", "voice", "open", None, 0),
        ("AMA Voice", "voice", "open", None, 0),
    ]),
    ("🎫 SUPPORT", [
        ("open-ticket", "text", "open", "Need help? Drop a message.", 0),
        ("bug-reports", "text", "open", "Found a bug? Tell us.", 0),
        ("suggestions", "text", "open", "Ideas to make NOVA Algo better.", 0),
        ("feedback", "text", "open", "What's working, what's not.", 0),
        ("contact-founder", "text", "open", "Direct line to Sir.", 0),
    ]),
    ("🔒 STAFF", [
        ("founder-notes", "text", "staff", "Founder-only scratch.", 0),
        ("bot-logs", "text", "staff", "All bot actions audited.", 0),
        ("signal-audit", "text", "staff", "Every signal webhook event.", 0),
        ("halt-events", "text", "staff", "Halt activations, fanout pauses.", 0),
        ("fanout-failures", "text", "staff", "Subscriber webhook delivery failures.", 0),
        ("partnership-inbox", "text", "staff", "Inbound partnership pings.", 0),
    ]),
]


intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


def _build_overwrites(guild, role_map, pattern, *, voice=False):
    everyone = guild.default_role
    bot = guild.me
    founder = role_map.get("Founder")
    co_founder = role_map.get("Co-Founder")
    marketing = role_map.get("Marketing")
    moderator = role_map.get("Moderator")

    if pattern == "open":
        return {}

    ow: dict = {}

    if pattern == "read_only":
        if voice:
            ow[everyone] = PermissionOverwrite(speak=False)
        else:
            ow[everyone] = PermissionOverwrite(
                send_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
            )
        ow[bot] = PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True,
            attach_files=True, manage_messages=True, manage_webhooks=True,
        )
        if founder:
            ow[founder] = PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True,
                attach_files=True, manage_messages=True,
            )
        if co_founder:
            ow[co_founder] = PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True,
                attach_files=True,
            )
        if marketing:
            ow[marketing] = PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True,
                attach_files=True,
            )
        if moderator:
            ow[moderator] = PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True,
            )
        return ow

    if pattern.startswith("tier:"):
        allowed_tiers = pattern[5:].split("|")
        ow[everyone] = PermissionOverwrite(view_channel=False)
        for tier in allowed_tiers:
            r = role_map.get(tier)
            if r:
                ow[r] = PermissionOverwrite(
                    view_channel=True, send_messages=True,
                    read_message_history=True, embed_links=True, attach_files=True,
                )
        for staff in (founder, co_founder, marketing, moderator):
            if staff:
                ow[staff] = PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                )
        ow[bot] = PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_messages=True, manage_webhooks=True,
        )
        return ow

    if pattern == "staff":
        ow[everyone] = PermissionOverwrite(view_channel=False)
        for staff in (founder, co_founder, marketing, moderator):
            if staff:
                ow[staff] = PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                )
        ow[bot] = PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_messages=True, manage_webhooks=True,
        )
        return ow

    return {}


async def configure_server(guild: discord.Guild):
    icon_path = os.path.join(os.path.dirname(__file__), "images", "server-icon.png")
    banner_path = os.path.join(os.path.dirname(__file__), "images", "server-banner.png")

    edits = {}
    if os.path.exists(icon_path) and not guild.icon:
        with open(icon_path, "rb") as f:
            edits["icon"] = f.read()
    if os.path.exists(banner_path) and "BANNER" in guild.features and not guild.banner:
        with open(banner_path, "rb") as f:
            edits["banner"] = f.read()

    if guild.verification_level == discord.VerificationLevel.none:
        edits["verification_level"] = discord.VerificationLevel.medium
    edits["default_notifications"] = discord.NotificationLevel.only_mentions

    if edits:
        try:
            await guild.edit(**edits, reason="NOVA Algo setup")
            print(f"  ✓ server settings: {', '.join(edits.keys())}")
        except discord.HTTPException as e:
            print(f"  ⚠ partial server-edit: {e}")
    else:
        print(f"  · server settings already configured")


async def create_roles(guild: discord.Guild) -> dict:
    role_map: dict = {}
    existing = {r.name: r for r in guild.roles}

    for name, color, hoist, mentionable in ROLES:
        if name in existing:
            role_map[name] = existing[name]
            print(f"  · {name} (exists)")
            continue
        try:
            role = await guild.create_role(
                name=name, color=color, hoist=hoist, mentionable=mentionable,
                reason="NOVA Algo setup",
            )
            role_map[name] = role
            print(f"  ✓ {name}")
        except discord.HTTPException as e:
            print(f"  ✗ {name} failed: {e}")
        await asyncio.sleep(0.25)

    # Reorder roles top-to-bottom (highest = highest position number, just below bot's role)
    try:
        bot_top_pos = guild.me.top_role.position
        positions = {}
        for i, (name, *_) in enumerate(ROLES):
            r = role_map.get(name)
            if not r:
                continue
            target = bot_top_pos - 1 - i
            if target > 0:
                positions[r] = target
        if positions:
            await guild.edit_role_positions(positions=positions)
            print(f"  ✓ reordered {len(positions)} roles")
    except discord.HTTPException as e:
        print(f"  ⚠ role reorder skipped: {e}")

    return role_map


async def create_categories_and_channels(guild, role_map):
    existing_cats = {c.name: c for c in guild.categories}
    existing_chans = {c.name: c for c in guild.channels}

    for cat_name, channels in CATEGORIES:
        if cat_name in existing_cats:
            cat = existing_cats[cat_name]
            print(f"  · {cat_name} (exists)")
        else:
            try:
                cat = await guild.create_category(name=cat_name, reason="NOVA Algo setup")
                print(f"  ✓ {cat_name}")
            except discord.HTTPException as e:
                print(f"  ✗ {cat_name} failed: {e}")
                continue
            await asyncio.sleep(0.3)

        for spec in channels:
            name, ctype, pattern, topic, slowmode = spec
            if name in existing_chans:
                print(f"      · #{name} (exists)")
                continue

            voice = ctype == "voice"
            overwrites = _build_overwrites(guild, role_map, pattern, voice=voice)

            try:
                if voice:
                    await guild.create_voice_channel(
                        name=name, category=cat, overwrites=overwrites,
                        reason="NOVA Algo setup",
                    )
                else:
                    await guild.create_text_channel(
                        name=name, category=cat, topic=topic or None,
                        slowmode_delay=slowmode, overwrites=overwrites,
                        reason="NOVA Algo setup",
                    )
                print(f"      ✓ #{name}")
            except discord.HTTPException as e:
                print(f"      ✗ #{name} failed: {e}")
            await asyncio.sleep(0.35)


async def cleanup_defaults(guild):
    our_names = set()
    for _, channels in CATEGORIES:
        for spec in channels:
            our_names.add(spec[0].lower())

    for ch in list(guild.channels):
        if ch.category is None:
            if ch.name.lower() == "general" and "general" in our_names:
                continue
            if ch.name.lower() in ("general", "general-voice", "voice", "off-topic"):
                if ch.name.lower() not in our_names:
                    try:
                        await ch.delete(reason="NOVA Algo setup — removing orphan default")
                        print(f"  ✓ removed orphan #{ch.name}")
                    except discord.HTTPException:
                        pass


async def post_welcome_content(guild):
    rules = discord.utils.get(guild.text_channels, name="rules")
    if rules and not [m async for m in rules.history(limit=1)]:
        embed = discord.Embed(
            title="📜 NOVA Algo — Server Rules",
            description=(
                "Welcome to **NOVA Algo** — auto-routed NY AM Opening Range Breakout for NQ futures.\n\n"
                "Read these rules before posting. Breaking them gets you warned, then removed.\n​"
            ),
            color=NOVA_CYAN,
        )
        embed.add_field(name="1. Be respectful", value="No harassment, slurs, doxxing, or personal attacks.", inline=False)
        embed.add_field(name="2. No financial advice", value="Share trades, share thoughts. Don't tell anyone what to do with their money.", inline=False)
        embed.add_field(name="3. No promotion", value="No outside services, signal groups, or affiliate links without explicit founder approval.", inline=False)
        embed.add_field(name="4. No scams", value="Asking for crypto, asking for trades for money, impersonating staff = instant ban.", inline=False)
        embed.add_field(name="5. Stay on-topic per channel", value="Off-topic chat goes in #off-topic. Trading talk goes in trading channels. See #server-guide.", inline=False)
        embed.add_field(name="6. NOVA Algo is the algo, not advice", value="Signals are the strategy's output. Your account, your decisions, your risk.", inline=False)
        embed.add_field(name="7. Respect tier-locked rooms", value="Don't ask people to share Fleet/Auto content publicly.", inline=False)
        embed.set_footer(text="Questions? → #contact-founder")
        await rules.send(embed=embed)
        print("  ✓ posted rules")

    welcome = discord.utils.get(guild.text_channels, name="welcome")
    if welcome and not [m async for m in welcome.history(limit=1)]:
        embed = discord.Embed(
            title="🌌 Welcome to NOVA Algo",
            description=(
                "**The auto-routed NY AM Opening Range Breakout algo for NQ futures.**\n\n"
                "Every weekday morning, NOVA fires NY AM ORB signals on NQ. The signal hits Railway, "
                "Railway fans out to TradersPost, TradersPost places trades on your prop firm or live account. "
                "You don't sit at the screen. You don't second-guess. You let it run.\n​"
            ),
            color=NOVA_CYAN,
        )
        embed.add_field(
            name="📋 Tier breakdown",
            value=(
                "🆓 **Beta** — Free forever. 15 spots. Full access.\n"
                "⭐ **Signal — $97/mo** — Live signals to your TradersPost webhook.\n"
                "🔥 **Auto — $297/mo** — Auto-routed to one prop or live account.\n"
                "💎 **Fleet — $997/mo** — Auto-routed to up to 5 accounts + Fleet vault."
            ),
            inline=False,
        )
        embed.add_field(
            name="🚀 First steps",
            value=(
                "1. Read **#rules** and **#server-guide**\n"
                "2. Pick your role in **#verify**\n"
                "3. Walk through **#onboarding-tradespost** to wire your webhook\n"
                "4. Watch **#morning-brief** at 8:00am ET\n"
                "5. Watch **#live-signals** at 8:30am ET when NY AM opens"
            ),
            inline=False,
        )
        embed.add_field(
            name="🌐 Links",
            value=(
                "Site: https://novaalgo.org\n"
                "Founder: founder@novaalgo.org\n"
                "Status: https://nova-production-72f5.up.railway.app/status"
            ),
            inline=False,
        )
        embed.set_footer(text="NOVA Algo · NY AM ORB · NQ futures · Auto-routed")
        await welcome.send(embed=embed)
        print("  ✓ posted welcome")

    guide = discord.utils.get(guild.text_channels, name="server-guide")
    if guide and not [m async for m in guide.history(limit=1)]:
        embed = discord.Embed(
            title="🗺️ Server guide",
            description="Every channel, what it's for, who can post.",
            color=NOVA_CYAN,
        )
        for cat_name, channels in CATEGORIES:
            lines = []
            for spec in channels:
                name, ctype, pattern, topic, _ = spec
                marker = "🔒" if pattern == "staff" else ("⭐" if pattern.startswith("tier:") else ("📢" if pattern == "read_only" else "💬"))
                if topic and ctype == "text":
                    lines.append(f"{marker} **#{name}** — {topic}")
                else:
                    lines.append(f"{marker} **{('#' if ctype == 'text' else '🔊 ')}{name}**")
            value = "\n".join(lines)
            if len(value) > 1024:
                value = value[:1020] + "..."
            embed.add_field(name=cat_name, value=value, inline=False)
        await guide.send(embed=embed)
        print("  ✓ posted server-guide")

    onboard = discord.utils.get(guild.text_channels, name="onboarding-tradespost")
    if onboard and not [m async for m in onboard.history(limit=1)]:
        embed = discord.Embed(
            title="🔌 TradersPost webhook setup",
            description="Wire your TradersPost so NOVA Algo can route trades to your prop or live account.",
            color=NOVA_CYAN,
        )
        embed.add_field(name="Step 1 — Sign up", value="https://traderspost.io · pick the plan that supports your prop firm or broker.", inline=False)
        embed.add_field(name="Step 2 — Connect broker/prop", value="Brokers → Add Broker → Apex / TopStep / Lucid / Tradovate / etc.", inline=False)
        embed.add_field(name="Step 3 — Create strategy", value="Strategies → Create Strategy → name it `NOVA-Algo-NQ`.", inline=False)
        embed.add_field(name="Step 4 — Copy webhook URL", value="In the strategy's webhooks tab, copy the unique URL.\nLooks like: `https://webhooks.traderspost.io/trading/webhook/abc/def`", inline=False)
        embed.add_field(name="Step 5 — Submit on novaalgo.org", value="https://novaalgo.org/portal → Subscriber Settings → paste webhook URL → Save. Sir approves within 24h, then you're live.", inline=False)
        embed.add_field(name="Step 6 — Verify", value="Watch #live-signals tomorrow at 8:30am ET. When NOVA fires, the trade hits TradersPost in seconds.", inline=False)
        embed.set_footer(text="Issues? → #open-ticket")
        await onboard.send(embed=embed)
        print("  ✓ posted onboarding-tradespost")


async def create_signal_webhooks(guild):
    targets = [
        ("live-signals", "NOVA Signals"),
        ("halt-events", "NOVA Halts"),
        ("fanout-failures", "NOVA Fanout"),
        ("equity-curve", "NOVA Equity"),
        ("morning-brief", "NOVA Morning"),
        ("eod-recap", "NOVA EOD"),
        ("status", "NOVA Status"),
    ]
    out_path = os.path.join(os.path.dirname(__file__), "WEBHOOK_URLS.md")
    lines = ["# Discord Webhook URLs", "",
             "Paste these into Railway env vars or your bridge bot config.",
             "Webhook URLs include a secret token — treat as credentials.", "", "```"]
    for ch_name, hook_name in targets:
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if not ch:
            continue
        try:
            existing = await ch.webhooks()
        except discord.Forbidden:
            print(f"  ⚠ no webhook perms on #{ch_name}")
            continue
        wh = next((w for w in existing if w.name == hook_name), None)
        if wh:
            print(f"  · {hook_name} → #{ch_name} (exists)")
        else:
            try:
                wh = await ch.create_webhook(name=hook_name, reason="NOVA Algo signal pipe")
                print(f"  ✓ {hook_name} → #{ch_name}")
            except discord.HTTPException as e:
                print(f"  ✗ {hook_name} → #{ch_name}: {e}")
                continue
        lines.append(f"DISCORD_{ch_name.upper().replace('-', '_')}_WEBHOOK_URL={wh.url}")
        await asyncio.sleep(0.2)
    lines += ["```", ""]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  ✓ wrote {out_path}")


@client.event
async def on_ready():
    print(f"\n✓ Logged in as {client.user} (ID: {client.user.id})")
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        print(f"\n❌ Bot is NOT in guild {GUILD_ID}.")
        print(f"   Invite the bot first via this URL, then re-run setup.py:")
        print(f"   https://discord.com/api/oauth2/authorize?client_id={client.user.id}&permissions=8&scope=bot%20applications.commands")
        await client.close()
        return

    print(f"✓ Connected to: {guild.name} (members: {guild.member_count})")
    print(f"  Bot top role pos: {guild.me.top_role.position}")
    print(f"  Bot has admin: {guild.me.guild_permissions.administrator}")
    if not guild.me.guild_permissions.administrator:
        print("\n❌ Bot lacks Administrator permission. Re-invite with permissions=8.")
        await client.close()
        return

    try:
        print("\n[1/6] Configuring server settings...")
        await configure_server(guild)
        print("\n[2/6] Creating roles...")
        role_map = await create_roles(guild)
        print("\n[3/6] Creating categories + channels...")
        await create_categories_and_channels(guild, role_map)
        print("\n[4/6] Posting welcome content...")
        await post_welcome_content(guild)
        print("\n[5/6] Cleaning up orphan defaults...")
        await cleanup_defaults(guild)
        print("\n[6/6] Generating signal webhooks...")
        await create_signal_webhooks(guild)
        print("\n✅ NOVA Algo Discord provisioned.")
        print(f"   Server: {guild.name}")
        print(f"   Channels: {len(guild.channels)}")
        print(f"   Roles: {len(guild.roles)}")
    except Exception as e:
        import traceback
        print(f"\n❌ Provisioning hit an error: {e}")
        traceback.print_exc()
    finally:
        await client.close()


def main():
    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env")
        sys.exit(1)
    if not GUILD_ID:
        print("ERROR: DISCORD_GUILD_ID not set in .env")
        sys.exit(1)
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
