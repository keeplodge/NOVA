"""
provision_extra_webhooks.py — create/lookup webhooks for the new daily-post channels.

Produces additions for WEBHOOK_URLS.md so Sir can paste into Railway env. Idempotent:
re-running just re-prints existing webhooks, never duplicates.

Channels provisioned:
  #key-levels             — daily 7:30 ET ICT levels card
  #news-feed              — daily 7:00 ET macro events
  #pre-market             — daily 9:00 ET bias snapshot + poll
  #trade-journal          — auto post-mortem on each closed trade
  #stats-dashboard        — nightly live-stats snapshot
  #milestones             — event-triggered (account flips, beta seats)
  #economic-calendar      — weekly macro digest
  #concept-of-the-week    — weekly education post
  #signal-audit           — every webhook event (staff)
  #bot-logs               — bot action audit (staff)

Usage:
  python provision_extra_webhooks.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import discord
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or "0")

NEW_HOOKS = [
    ("key-levels",          "NOVA Key Levels"),
    ("news-feed",           "NOVA News Feed"),
    ("pre-market",          "NOVA Pre-Market"),
    ("trade-journal",       "NOVA Trade Journal"),
    ("stats-dashboard",     "NOVA Stats Dashboard"),
    ("milestones",          "NOVA Milestones"),
    ("economic-calendar",   "NOVA Calendar"),
    ("concept-of-the-week", "NOVA Concept"),
    ("signal-audit",        "NOVA Signal Audit"),
    ("bot-logs",            "NOVA Bot Logs"),
]

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[provision] connected as {client.user}", flush=True)
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print(f"[provision] guild {GUILD_ID} not found", flush=True)
        await client.close()
        return

    out_lines = ["", "## Phase 1 — additional channel webhooks", "", "```"]

    for ch_name, hook_name in NEW_HOOKS:
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if not ch:
            print(f"  [skip] #{ch_name} not found in guild", flush=True)
            continue
        try:
            existing = await ch.webhooks()
        except discord.Forbidden:
            print(f"  [warn] no webhook perms on #{ch_name}", flush=True)
            continue
        wh = next((w for w in existing if w.name == hook_name), None)
        if wh:
            print(f"  [exists] {hook_name} -> #{ch_name}", flush=True)
        else:
            try:
                wh = await ch.create_webhook(name=hook_name, reason="NOVA Algo Phase 1 daily-posts")
                print(f"  [ok]     {hook_name} -> #{ch_name}", flush=True)
            except discord.HTTPException as e:
                print(f"  [fail]   {hook_name} -> #{ch_name}: {e}", flush=True)
                continue
        env_key = f"DISCORD_{ch_name.upper().replace('-', '_')}_WEBHOOK_URL"
        out_lines.append(f"{env_key}={wh.url}")
        await asyncio.sleep(0.2)

    out_lines += ["```", ""]
    out_path = os.path.join(os.path.dirname(__file__), "WEBHOOK_URLS.md")
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        existing = ""
    if "## Phase 1 — additional channel webhooks" in existing:
        # Remove the prior block so we keep the file clean on re-run.
        head = existing.split("## Phase 1 — additional channel webhooks", 1)[0].rstrip()
        existing = head + "\n"
    new_content = (existing.rstrip() + "\n" + "\n".join(out_lines)).rstrip() + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"  [ok] updated {out_path}", flush=True)

    await client.close()


def main():
    if not TOKEN or not GUILD_ID:
        print("DISCORD_BOT_TOKEN / DISCORD_GUILD_ID missing", flush=True)
        sys.exit(1)
    asyncio.run(client.start(TOKEN))


if __name__ == "__main__":
    main()
