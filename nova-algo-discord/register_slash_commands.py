"""Register the NOVA Algo slash commands with Discord (per-guild for instant availability)."""
import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
APP_ID = os.environ["DISCORD_APP_ID"]
GUILD_ID = os.environ["DISCORD_GUILD_ID"]

COMMANDS = [
    {
        "name": "status",
        "description": "Show current NOVA Algo trading status (sessions, trades, equity).",
        "type": 1,
    },
    {
        "name": "sizer",
        "description": "Position sizing reference for NQ futures at NOVA's standard risk.",
        "type": 1,
    },
    {
        "name": "levels",
        "description": "Today's key levels and last fired signal reference.",
        "type": 1,
    },
]

url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
r = requests.put(url, headers={"Authorization": f"Bot {TOKEN}"}, json=COMMANDS, timeout=10)
print(f"HTTP {r.status_code}")
if r.ok:
    for c in r.json():
        print(f"  ✓ /{c['name']} — {c['description']}")
else:
    print(r.text[:500])
