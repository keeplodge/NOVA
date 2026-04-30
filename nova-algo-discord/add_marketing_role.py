"""
add_marketing_role.py — one-shot. Creates the "Marketing" role in NOVA Algo
Discord (pink, hoisted, mentionable). Idempotent: re-running just confirms
the role exists. Reorders roles top-to-bottom afterwards.
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

ROLE_NAME = "Marketing"
ROLE_COLOR = Color(0xEC4899)  # pink-500
HOIST = True
MENTIONABLE = True

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print("[role] guild not found", flush=True)
        await client.close()
        return

    existing = discord.utils.get(guild.roles, name=ROLE_NAME)
    if existing:
        print(f"[role] {ROLE_NAME} already exists (id={existing.id})", flush=True)
        role = existing
    else:
        try:
            role = await guild.create_role(
                name=ROLE_NAME,
                color=ROLE_COLOR,
                hoist=HOIST,
                mentionable=MENTIONABLE,
                reason="NOVA Algo: marketing lead role for Victor",
            )
            print(f"[role] created {ROLE_NAME} (id={role.id})", flush=True)
        except discord.HTTPException as e:
            print(f"[role] create failed: {e}", flush=True)
            await client.close()
            return

    # Position: just below Co-Founder, above Moderator
    co_founder = discord.utils.get(guild.roles, name="Co-Founder")
    moderator = discord.utils.get(guild.roles, name="Moderator")
    if co_founder and moderator:
        target_pos = co_founder.position - 1
        if role.position != target_pos:
            try:
                await guild.edit_role_positions(positions={role: target_pos})
                print(f"[role] positioned {ROLE_NAME} at {target_pos} "
                      f"(below Co-Founder@{co_founder.position}, "
                      f"above Moderator@{moderator.position})", flush=True)
            except discord.HTTPException as e:
                print(f"[role] reposition failed: {e}", flush=True)

    print(f"[role] DONE — Marketing role ready. Right-click Victor → Roles → "
          f"check 'Marketing' to assign.", flush=True)
    await client.close()


def main():
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
