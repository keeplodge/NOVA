"""Generate a permanent Discord invite for the NOVA Algo server."""
import asyncio
import os
import discord
from dotenv import load_dotenv

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
        print("guild not found"); await client.close(); return

    # Try welcome channel first, fall back to any text channel we can post in
    welcome = discord.utils.get(guild.text_channels, name="welcome")
    target = welcome or next((c for c in guild.text_channels if c.permissions_for(guild.me).create_instant_invite), None)
    if not target:
        print("no postable channel found"); await client.close(); return

    # Reuse an existing infinite invite if one exists
    try:
        existing = await guild.invites()
        infinite = next((i for i in existing if i.max_age == 0 and i.max_uses == 0), None)
    except discord.HTTPException:
        infinite = None

    if infinite:
        print(f"existing permanent invite: {infinite.url}")
    else:
        try:
            invite = await target.create_invite(
                max_age=0,        # never expires
                max_uses=0,       # unlimited uses
                unique=False,
                reason="NOVA Algo public site CTA",
            )
            print(f"created permanent invite: {invite.url}")
        except discord.HTTPException as e:
            print(f"invite creation failed: {e}")

    await client.close()


client.run(TOKEN, log_handler=None)
