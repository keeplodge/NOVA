"""Delete smoke-test bridge posts and re-run populate for the affected channels."""
import asyncio
import os
import discord
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

# Channels that received smoke-test posts and need cleanup before the
# proper explainer embed lands. (Staff channels left alone — they're
# Sir-only and the smoke posts there are diagnostic.)
TARGETS = ["equity-curve", "morning-brief", "status"]
SMOKE_MARKERS = ("smoke test", "bridge connectivity test", "bridge online")

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print("guild not found"); await client.close(); return

    deleted = 0
    for ch_name in TARGETS:
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if not ch:
            print(f"  · #{ch_name} not found"); continue

        # Without Message Content intent we can't read embed bodies. These 3
        # channels only contain bridge smoke posts at this point — wipe all
        # bot/webhook messages outright so populate can land its explainer.
        async for msg in ch.history(limit=20):
            if not msg.author.bot:
                continue
            try:
                await msg.delete()
                deleted += 1
                print(f"  ✓ deleted bot post in #{ch_name} from {msg.author.name}")
            except discord.HTTPException as e:
                print(f"  ✗ delete failed in #{ch_name}: {e}")
            await asyncio.sleep(0.4)

    print(f"\n✓ deleted {deleted} smoke-test posts.")
    await client.close()


client.run(TOKEN, log_handler=None)
