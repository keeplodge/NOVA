"""Print the role IDs we need to wire into the tier-sync API."""
import os, discord
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)

WANTED = ("Founder", "Co-Founder", "Moderator", "Coach", "Fleet", "Auto", "Signal",
          "Beta", "Verified", "TradersPost Connected", "Funded", "Apex", "TopStep",
          "Lucid", "Member")

@client.event
async def on_ready():
    g = client.get_guild(GUILD_ID)
    print(f"GUILD_ID={g.id}")
    print(f"GUILD_NAME={g.name}")
    print()
    for name in WANTED:
        role = discord.utils.get(g.roles, name=name)
        if role:
            print(f"DISCORD_ROLE_{name.upper().replace(' ', '_').replace('-', '_')}={role.id}")
        else:
            print(f"# {name}: NOT FOUND")
    await client.close()

client.run(TOKEN, log_handler=None)
