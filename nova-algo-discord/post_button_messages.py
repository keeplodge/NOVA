"""
post_button_messages.py — post the interactive button messages.

  • #open-ticket  → "Click to open a private ticket" button
  • #verify       → role-flair selectors (prop firm + funded toggles)

Idempotent — skips if a button message from us already exists.
"""
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


async def post_ticket_button(guild):
    ch = discord.utils.get(guild.text_channels, name="open-ticket")
    if not ch:
        print("  ✗ #open-ticket not found"); return

    # Check if a message with the right component already exists
    async for m in ch.history(limit=10):
        if m.author.id == client.user.id and m.components:
            print("  · #open-ticket button already posted")
            return

    embed = discord.Embed(
        title="🎫 Need help? Open a ticket.",
        description=(
            "Click the button below — a **private thread** spins up for you and a staff member.\n\n"
            "**Use a ticket for:**\n"
            "• TradersPost webhook isn't firing\n"
            "• Missing signal you should have received\n"
            "• Billing or account issues\n"
            "• Anything you don't want public\n\n"
            "**Don't use a ticket for:** general questions (try `#ask-a-coach`) or feature requests (`#suggestions`)."
        ),
        color=discord.Color(0xF59E0B),
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.primary,
        label="Open a ticket",
        emoji="🎫",
        custom_id="ticket:open",
    ))
    await ch.send(embed=embed, view=view)
    print("  ✓ ticket button posted")


async def post_flair_buttons(guild):
    ch = discord.utils.get(guild.text_channels, name="verify")
    if not ch:
        print("  ✗ #verify not found"); return

    # Look for an existing buttons message
    async for m in ch.history(limit=15):
        if m.author.id == client.user.id and m.components:
            print("  · #verify flair buttons already posted")
            return

    embed = discord.Embed(
        title="🏛️ Pick your prop firm flair",
        description=(
            "Tap the buttons below to add or remove flair. Toggle as many as apply.\n\n"
            "**Tier roles** (Beta/Signal/Auto/Fleet) are assigned automatically when you "
            "subscribe + link your Discord on novaalgo.org/portal. Don't ask staff for them.\n\n"
            "**Funded** = self-attested if you currently hold a live funded account."
        ),
        color=discord.Color(0x00F5D4),
    )
    # Discord allows max 5 buttons per row, max 5 rows = 25 total
    rows = [
        # Row 1 — major US futures props
        [
            ("flair:apex",       "Apex",          discord.ButtonStyle.secondary),
            ("flair:topstep",    "TopStep",       discord.ButtonStyle.secondary),
            ("flair:lucid",      "Lucid",         discord.ButtonStyle.secondary),
            ("flair:tradeify",   "Tradeify",      discord.ButtonStyle.secondary),
            ("flair:tpt",        "TakeProfitTrader", discord.ButtonStyle.secondary),
        ],
        # Row 2
        [
            ("flair:toponetrader", "TopOne",      discord.ButtonStyle.secondary),
            ("flair:mff",          "MyFundedFutures", discord.ButtonStyle.secondary),
            ("flair:earn2trade",   "Earn2Trade",  discord.ButtonStyle.secondary),
            ("flair:bulenox",      "Bulenox",     discord.ButtonStyle.secondary),
            ("flair:tradingpit",   "TradingPit",  discord.ButtonStyle.secondary),
        ],
        # Row 3
        [
            ("flair:fundednext", "FundedNext",   discord.ButtonStyle.secondary),
            ("flair:aquafunded", "AquaFunded",   discord.ButtonStyle.secondary),
            ("flair:funded",     "💰 Funded",     discord.ButtonStyle.success),
        ],
    ]
    view = discord.ui.View(timeout=None)
    for row in rows:
        for cid, label, style in row:
            view.add_item(discord.ui.Button(custom_id=cid, label=label, style=style))
    await ch.send(embed=embed, view=view)
    print("  ✓ flair buttons posted")


@client.event
async def on_ready():
    g = client.get_guild(GUILD_ID)
    if not g:
        print("guild not found"); await client.close(); return
    print(f"Connected to {g.name}\n")

    print("[1/2] Posting ticket button to #open-ticket...")
    await post_ticket_button(g)

    print("\n[2/2] Posting flair buttons to #verify...")
    await post_flair_buttons(g)

    print("\n✓ done.")
    await client.close()


client.run(TOKEN, log_handler=None)
