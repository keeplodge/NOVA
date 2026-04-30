"""
refresh_channel_perms.py — re-apply permission overwrites across all existing
channels. Used after a role addition (like Marketing) to grant the role its
proper post/view permissions on already-provisioned channels.

Idempotent: just re-runs the same perm spec from setup.py against every channel.
"""
from __future__ import annotations

import asyncio
import os

import discord
from dotenv import load_dotenv

# Reuse setup.py's CATEGORIES + _build_overwrites + ROLES so we stay in sync.
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup import CATEGORIES, _build_overwrites, ROLES  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print("[perms] guild not found", flush=True)
        await client.close()
        return
    print(f"[perms] connected to {guild.name}", flush=True)

    role_map = {r.name: r for r in guild.roles}
    refreshed = 0
    skipped = 0
    failed = 0

    for cat_name, channels in CATEGORIES:
        for spec in channels:
            ch_name, ctype, pattern, _topic, _slowmode = spec
            voice = ctype == "voice"
            ch = (
                discord.utils.get(guild.voice_channels, name=ch_name) if voice
                else discord.utils.get(guild.text_channels, name=ch_name)
            )
            if not ch:
                print(f"  [skip] #{ch_name} (not found)", flush=True)
                skipped += 1
                continue
            overwrites = _build_overwrites(guild, role_map, pattern, voice=voice)
            try:
                await ch.edit(overwrites=overwrites, reason="Refresh perms — Marketing role")
                print(f"  [ok] #{ch_name} ({pattern})", flush=True)
                refreshed += 1
            except discord.Forbidden:
                print(f"  [warn] no manage_channels on #{ch_name}", flush=True)
                failed += 1
            except discord.HTTPException as e:
                print(f"  [fail] #{ch_name}: {e}", flush=True)
                failed += 1
            await asyncio.sleep(0.4)

    print(f"\n[perms] refreshed {refreshed} · skipped {skipped} · failed {failed}", flush=True)
    await client.close()


def main():
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
