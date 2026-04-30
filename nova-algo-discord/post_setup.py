"""
post_setup.py — supplemental setup tasks the main provisioner skipped.

- Force-applies server icon (main script gates on `not guild.icon` which fails
  if Discord auto-assigned a placeholder).
- Drops the launch announcement in #announcements.
- Drops the verify-role reaction message in #verify.
- Drops the live-signals welcome marker in #live-signals.
"""
from __future__ import annotations

import asyncio
import os
import sys

import discord
from discord import Color
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
NOVA_CYAN = Color(0x00F5D4)

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        print("guild not found"); await client.close(); return

    print(f"Connected to {guild.name}")

    # 1. Force server icon
    icon_path = os.path.join(os.path.dirname(__file__), "images", "server-icon.png")
    if os.path.exists(icon_path):
        try:
            with open(icon_path, "rb") as f:
                await guild.edit(icon=f.read(), reason="NOVA Algo branding")
            print("✓ icon set")
        except discord.HTTPException as e:
            print(f"⚠ icon edit failed: {e}")

    # 2. Try banner — only works if guild has Community / boost level 1+
    banner_path = os.path.join(os.path.dirname(__file__), "images", "server-banner.png")
    if "BANNER" in guild.features and os.path.exists(banner_path):
        try:
            with open(banner_path, "rb") as f:
                await guild.edit(banner=f.read(), reason="NOVA Algo banner")
            print("✓ banner set")
        except discord.HTTPException as e:
            print(f"⚠ banner edit failed: {e}")
    else:
        print("· banner skipped (server needs Community Server enabled OR boost level 1+)")

    # 3. Launch post in #announcements
    announce = discord.utils.get(guild.text_channels, name="announcements")
    if announce and not [m async for m in announce.history(limit=1)]:
        embed = discord.Embed(
            title="🚀 NOVA Algo is live.",
            description=(
                "Beta launch is **active**. The algo, the server, the signals — all live as of today.\n\n"
                "**What you can expect, every weekday:**\n"
                "• 8:00am ET — `#morning-brief` posts NY AM levels, bias, conditions\n"
                "• 8:30am ET — NY AM session opens, `#live-signals` fires when NOVA triggers\n"
                "• Any fired trade auto-posts to your TradersPost (if you've onboarded)\n"
                "• 11:00am ET — session closes, `#eod-recap` posts P&L\n\n"
                "**Beta seats:** 15 free forever. Onboard via `#onboarding-tradespost`.\n"
                "**Site:** https://novaalgo.org\n​"
            ),
            color=NOVA_CYAN,
        )
        embed.set_footer(text="Founder · NOVA Algo")
        try:
            await announce.send(embed=embed)
            print("✓ posted launch announcement")
        except discord.HTTPException as e:
            print(f"⚠ announcement failed: {e}")

    # 4. Verify channel — pick-your-tier message
    verify = discord.utils.get(guild.text_channels, name="verify")
    if verify and not [m async for m in verify.history(limit=1)]:
        embed = discord.Embed(
            title="🛂 Pick your role",
            description=(
                "Tell us what you are so the right channels open up.\n\n"
                "**Tier roles** — we assign these manually after onboarding (paid tiers) "
                "or when you grab a beta seat. Don't self-assign tier roles.\n\n"
                "**Self-assign these (DM @Founder or post here):**\n"
                "🏛️ `Apex` — you trade Apex prop\n"
                "🏛️ `TopStep` — you trade TopStep\n"
                "🏛️ `Lucid` — you trade Lucid\n"
                "💰 `Funded` — you have live funded capital\n"
                "✅ `Verified` — auto-assigned after onboarding\n\n"
                "Once a tier role is on you, the corresponding **#{tier}-lounge** channel unlocks."
            ),
            color=NOVA_CYAN,
        )
        embed.set_footer(text="Founder will assign tier roles. Stay patient.")
        try:
            await verify.send(embed=embed)
            print("✓ posted verify intro")
        except discord.HTTPException as e:
            print(f"⚠ verify failed: {e}")

    # 5. Live-signals placeholder
    live = discord.utils.get(guild.text_channels, name="live-signals")
    if live and not [m async for m in live.history(limit=1)]:
        embed = discord.Embed(
            title="🔔 Live signals — feed armed",
            description=(
                "This channel auto-populates from Railway every time NOVA Algo fires a NY AM ORB signal on NQ.\n\n"
                "Format per signal:\n"
                "• Side (LONG / SHORT)\n"
                "• Entry, SL, TP\n"
                "• Risk in $ (per contract)\n"
                "• Account fanout status (which TradersPost endpoints accepted it)\n\n"
                "First fire of the day usually lands between 8:30am and 9:30am ET."
            ),
            color=NOVA_CYAN,
        )
        embed.set_footer(text="Read-only · pipe = Railway → Discord webhook")
        try:
            await live.send(embed=embed)
            print("✓ posted live-signals marker")
        except discord.HTTPException as e:
            print(f"⚠ live-signals failed: {e}")

    print("\ndone.")
    await client.close()


def main():
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
