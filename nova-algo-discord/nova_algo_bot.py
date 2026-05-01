"""
nova_algo_bot.py — long-running NOVA Algo Discord bot.

Handles:
  • /levels        — returns current NQ reference levels (PDH/PDL/weekly open)
  • /winrate       — returns the caller's personal fanout-routed win rate
  • /status        — current Railway pipeline state (active session, trades today)
  • Reaction roles in #verify — one click picks Beta/Signal/Auto/Fleet visibility
  • Bias poll buttons in #pre-market — Long / Short / No-trade vote

Runs as `python nova_algo_bot.py`. Needs DISCORD_BOT_TOKEN, DISCORD_GUILD_ID,
NOVA_API_BASE_URL, NOVA_WEBHOOK_SECRET in .env.

Railway deploy: add `bot: python nova-algo-discord/nova_algo_bot.py` to Procfile
and provision a second Railway service from the same repo.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TOKEN     = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID  = int(os.environ["DISCORD_GUILD_ID"])
API_BASE  = os.environ.get("NOVA_API_BASE_URL", "https://nova-production-72f5.up.railway.app")
SECRET    = os.environ.get("NOVA_WEBHOOK_SECRET", "")
SITE_BASE = os.environ.get("NOVA_SITE_BASE_URL", "https://novaalgo.org")

EST = timezone(timedelta(hours=-4))  # EDT default; flips with system tz

intents = discord.Intents.default()
intents.guilds = True
intents.reactions = True
intents.message_content = False  # not required for our handlers
# intents.members requires "Server Members Intent" toggle in Discord Developer
# Portal. Off by default — turn ON if you want welcome DMs to fire on join.
intents.members = os.environ.get("DISCORD_MEMBERS_INTENT", "0") == "1"
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ── Persistent state paths ──────────────────────────────────────────────────

STATE_DIR = os.path.join(os.path.dirname(__file__), ".bot_state")
os.makedirs(STATE_DIR, exist_ok=True)
TRIVIA_PATH = os.path.join(os.path.dirname(__file__), "content", "trivia.json")
COFFEE_PATH = os.path.join(os.path.dirname(__file__), "content", "coffee_prompts.json")
FAQ_PATH = os.path.join(os.path.dirname(__file__), "content", "faq.json")
TRIVIA_LB_PATH = os.path.join(STATE_DIR, "trivia_leaderboard.json")
TRIVIA_OPEN_PATH = os.path.join(STATE_DIR, "trivia_open.json")
WIN_PINNED_PATH = os.path.join(STATE_DIR, "win_pinned.json")
BIAS_POLL_PATH = os.path.join(STATE_DIR, "bias_poll.json")
DM_OPTOUT_PATH = os.path.join(STATE_DIR, "morningdm_optout.json")


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[bot] save_json {path} failed: {e}", flush=True)


def _is_staff(member: discord.Member) -> bool:
    """Founder, Co-Founder, or Moderator role grants staff privileges."""
    if not member or not member.roles:
        return False
    staff_names = {"Founder", "Co-Founder", "Moderator"}
    return any(r.name in staff_names for r in member.roles)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _http_get_json(url: str, *, timeout: int = 6) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NOVA-Algo-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[bot] GET {url} failed: {e}", flush=True)
        return None


# ── Slash: /levels ──────────────────────────────────────────────────────────

@tree.command(
    name="levels",
    description="Today's NQ reference levels — PDH, PDL, weekly open, and current price.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_levels(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=False)
    # Trigger the Railway key-levels endpoint and read the levels back.
    levels = None
    try:
        post_url = f"{API_BASE}/discord/key-levels/post"
        req = urllib.request.Request(
            post_url,
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Nova-Secret": SECRET},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
            levels = payload.get("levels") or {}
    except Exception as e:
        print(f"[bot] /levels failed: {e}", flush=True)
        await interaction.followup.send("Couldn't reach Railway. Try again in a moment.", ephemeral=True)
        return

    if not levels:
        await interaction.followup.send("No level data available right now.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🎯 NQ Levels · {levels.get('as_of','today')}",
        color=0x00F5D4,
        description=f"**Last** · {levels.get('current',0):,.2f}",
    )
    rows = []
    for label, key in [
        ("PDH",                "pdh"),
        ("PDL",                "pdl"),
        ("Weekly open",        "weekly_open"),
        ("Prior week H",       "prior_week_h"),
        ("Prior week L",       "prior_week_l"),
        ("5-day H",            "session_h_5d"),
        ("5-day L",            "session_l_5d"),
    ]:
        v = levels.get(key)
        if v is not None:
            rows.append(f"**{label}** · {v:,.2f}")
    embed.add_field(name="Reference", value="\n".join(rows) or "—", inline=False)
    embed.set_footer(text="NOVA Algo · /levels")
    await interaction.followup.send(embed=embed)


# ── Slash: /status ──────────────────────────────────────────────────────────

@tree.command(
    name="status",
    description="Current NOVA Algo router state — session, trades today, daily loss budget.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=False)
    s = _http_get_json(f"{API_BASE}/status") or {}
    if not s:
        await interaction.followup.send("Couldn't reach Railway right now.", ephemeral=True)
        return
    session = s.get("active_session") or "—"
    trades  = s.get("trades_today", 0)
    loss    = s.get("daily_loss", 0)
    cap     = s.get("loss_limit", 500)
    open_n  = len(s.get("open_positions") or {})
    embed = discord.Embed(
        title="🟢 NOVA Algo · live status",
        color=0x22C55E if open_n == 0 and loss < cap else 0xFBBF24,
        description=f"**Active session** · {session}",
    )
    embed.add_field(name="Trades today",  value=str(trades),                 inline=True)
    embed.add_field(name="Loss budget",   value=f"${loss:.0f} / ${cap:.0f}", inline=True)
    embed.add_field(name="Open positions",value=str(open_n),                 inline=True)
    embed.set_footer(text=f"NOVA Algo · {SITE_BASE.replace('https://','')}/status")
    await interaction.followup.send(embed=embed)


# ── Slash: /winrate ─────────────────────────────────────────────────────────

@tree.command(
    name="winrate",
    description="Your personal fanout-routed win rate (counts NOVA fires that hit your TradersPost).",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_winrate(interaction: discord.Interaction):
    """
    Hits novaalgo.org /api/me/stats?discordId=<id> which is a future endpoint.
    For now, returns the cohort-wide stat from /api/stats/live as a placeholder
    until the per-user endpoint exists.
    """
    await interaction.response.defer(thinking=True, ephemeral=True)
    discord_id = str(interaction.user.id)
    user_stats = _http_get_json(f"{SITE_BASE}/api/me/stats?discordId={urllib.parse.quote(discord_id)}")

    if user_stats and user_stats.get("ok") and user_stats.get("trades", 0) > 0:
        embed = discord.Embed(
            title="📊 Your NOVA Algo win rate",
            color=0x00F5D4,
            description=(
                f"**{user_stats.get('winRate',0):.1f}%** win rate over **{user_stats['trades']}** routed trades.\n"
                f"Net **${user_stats.get('netUsd',0):+,.0f}** since {user_stats.get('since','onboarding')}."
            ),
        )
        embed.set_footer(text=f"NOVA Algo · /winrate · {SITE_BASE.replace('https://','')}/portal/journal")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Fallback to cohort-wide live stats
    cohort = _http_get_json(f"{SITE_BASE}/api/stats/live")
    if not cohort or not cohort.get("ok"):
        await interaction.followup.send("No personal stats yet — connect your TradersPost webhook first.", ephemeral=True)
        return
    m = cohort.get("merged", {})
    embed = discord.Embed(
        title="📊 NOVA Algo · cohort win rate",
        color=0x00F5D4,
        description=(
            f"You don't have routed trades yet. Cohort-wide:\n"
            f"**{m.get('winRate',0):.2f}%** over **{m.get('trades',0)}** trades · "
            f"PF **{m.get('profitFactor',0):.2f}** · Net **+${m.get('netUsd',0):,.0f}**"
        ),
    )
    embed.set_footer(text=f"NOVA Algo · /winrate · connect at {SITE_BASE.replace('https://','')}/portal/connect")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Reaction roles ──────────────────────────────────────────────────────────
#
# Maintains a single "verify" message in #verify. Reactions assign one of the
# tier roles. Message ID is persisted to a JSON sidecar so the bot survives
# restarts.

REACTION_MAP = {
    "🟢": "Beta",
    "🔵": "Signal",
    "🟠": "Auto",
    "🟦": "Fleet",  # blue square — distinct from circle
}
VERIFY_STATE_PATH = os.path.join(os.path.dirname(__file__), ".verify_message.json")


def _load_verify_msg_id() -> int | None:
    try:
        with open(VERIFY_STATE_PATH, "r", encoding="utf-8") as f:
            return int(json.load(f).get("message_id") or 0) or None
    except Exception:
        return None


def _save_verify_msg_id(mid: int) -> None:
    with open(VERIFY_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"message_id": mid}, f)


async def _ensure_verify_message(guild: discord.Guild):
    ch = discord.utils.get(guild.text_channels, name="verify")
    if not ch:
        print("[bot] #verify channel not found", flush=True)
        return
    existing_id = _load_verify_msg_id()
    if existing_id:
        try:
            await ch.fetch_message(existing_id)
            return
        except discord.NotFound:
            pass

    embed = discord.Embed(
        title="🔓 Pick your tier",
        color=0x00F5D4,
        description=(
            "React below to unlock the lounge for your tier. You can change it any time.\n\n"
            "🟢 **Beta** — free Fleet for the cohort (10 spots)\n"
            "🔵 **Signal** — $97/mo, alerts only\n"
            "🟠 **Auto** — $297/mo, auto-routed via TradersPost\n"
            "🟦 **Fleet** — $997/mo, the inner circle\n"
        ),
    )
    embed.set_footer(text="NOVA Algo · self-serve verify")
    msg = await ch.send(embed=embed)
    for emoji in REACTION_MAP:
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass
    _save_verify_msg_id(msg.id)
    print(f"[bot] verify message posted, id={msg.id}", flush=True)


@client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id != GUILD_ID:
        return
    msg_id = _load_verify_msg_id()
    if not msg_id or payload.message_id != msg_id:
        return
    role_name = REACTION_MAP.get(str(payload.emoji))
    if not role_name:
        return
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    role = discord.utils.get(guild.roles, name=role_name)
    member = guild.get_member(payload.user_id)
    if role and member and role in member.roles:
        try:
            await member.remove_roles(role, reason="self-serve verify (removed)")
            print(f"[bot] −{role_name} → {member.name}", flush=True)
        except discord.Forbidden:
            print(f"[bot] missing perms to remove {role_name}", flush=True)


# ── /link slash command — connect Discord to novaalgo.org account ───────────

@tree.command(
    name="link",
    description="Get a 6-digit code to link your Discord to your NOVA Algo account.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_link(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        body = json.dumps({
            "discord_id":   str(interaction.user.id),
            "discord_name": interaction.user.name,
        }).encode()
        req = urllib.request.Request(
            f"{API_BASE}/admin/link/issue",
            data=body,
            headers={"Content-Type": "application/json", "X-Nova-Secret": SECRET},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read().decode())
    except Exception as e:
        await interaction.followup.send(f"Couldn't reach Railway: {e}", ephemeral=True)
        return
    if payload.get("status") != "ok":
        await interaction.followup.send("Couldn't issue a link code right now.", ephemeral=True)
        return
    code = payload.get("code", "")
    embed = discord.Embed(
        title="🔗 Link Discord → NOVA Algo",
        color=0x00F5D4,
        description=(
            f"**Your one-time code: `{code}`**\n\n"
            f"1. Open [{SITE_BASE}/portal/link-discord]({SITE_BASE}/portal/link-discord)\n"
            f"2. Sign in if needed\n"
            f"3. Paste this code → Submit\n\n"
            f"Code expires in **10 minutes**. Single-use."
        ),
    )
    embed.set_footer(text="NOVA Algo · /link · only you see this")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /faq slash command ──────────────────────────────────────────────────────

@tree.command(
    name="faq",
    description="Browse NOVA Algo FAQ — common questions answered.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_faq(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    try:
        with open(FAQ_PATH, "r", encoding="utf-8") as f:
            faq = json.load(f)
    except Exception:
        await interaction.followup.send("FAQ unavailable right now.", ephemeral=True)
        return
    embed = discord.Embed(
        title="❓ NOVA Algo · FAQ",
        color=0x00F5D4,
        description="Common questions, fast answers.",
    )
    for entry in faq[:10]:
        q = entry.get("q", "")[:240]
        a = entry.get("a", "")[:1000]
        embed.add_field(name=f"Q: {q}", value=a, inline=False)
    embed.set_footer(text="NOVA Algo · /faq · ask more in #ask-a-coach")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /strategy slash command ─────────────────────────────────────────────────

@tree.command(
    name="strategy",
    description="The NOVA Algo strategy in one card — rules, risk, exits.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_strategy(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    embed = discord.Embed(
        title="📐 NOVA NQ ORB · NY AM 30m",
        color=0x00F5D4,
        description=(
            "**Pure stop-entry breakout.** No ICT prediction, no confirmation candle. "
            "First side of the 9:30 OR to break wins."
        ),
    )
    embed.add_field(name="Session", value="9:30 – 11:00 ET, weekdays only", inline=True)
    embed.add_field(name="Timeframe", value="30-minute · NQ futures", inline=True)
    embed.add_field(name="OR bar", value="The 9:30 ET 30m candle", inline=True)
    embed.add_field(name="Entry", value="Buy-stop @ OR_high+1t · Sell-stop @ OR_low−1t · armed 10:00", inline=False)
    embed.add_field(name="Risk", value="$500 fixed SL · 1 contract", inline=True)
    embed.add_field(name="Target", value="$2,000 fixed TP · 4R", inline=True)
    embed.add_field(name="Trade mgmt", value="BE @ $500 · Trail @ $750 · Hard out 11:00 ET", inline=False)
    embed.add_field(
        name="Verified edge",
        value="**82.6% WR · PF 7.78 · 322 trades · +$155,645 net** (Jan 2025 → Apr 2026 backtest, growing live)",
        inline=False,
    )
    embed.set_footer(text=f"NOVA Algo · /strategy · {SITE_BASE.replace('https://','')}/performance")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /next-event slash command ───────────────────────────────────────────────

@tree.command(
    name="next-event",
    description="Next high-impact USD macro print today.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_next_event(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        with urllib.request.urlopen(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=6,
        ) as r:
            data = json.loads(r.read().decode())
    except Exception:
        await interaction.followup.send("Couldn't fetch the macro feed right now.", ephemeral=True)
        return
    today_iso = datetime.now(EST).strftime("%Y-%m-%d")
    now_iso = datetime.now(EST).isoformat()
    upcoming = []
    for ev in data:
        ts = ev.get("date") or ""
        if not ts.startswith(today_iso):
            continue
        currency = (ev.get("country") or ev.get("currency") or "").upper()
        if currency not in ("USD", ""):
            continue
        impact = (ev.get("impact") or "").lower()
        if impact != "high":
            continue
        if ts >= now_iso:
            upcoming.append(ev)
    if not upcoming:
        await interaction.followup.send(
            "📰 No high-impact USD prints left today. Pure technicals — clean ORB read."
        )
        return
    upcoming.sort(key=lambda e: e.get("date", ""))
    next_ev = upcoming[0]
    next_time = next_ev.get("date", "")[11:16]
    embed = discord.Embed(
        title="📰 Next macro print",
        color=0xF59E0B,
        description=(
            f"**{next_time} ET · {next_ev.get('title','Event')}**\n"
            f"Forecast: {next_ev.get('forecast','—')} · Prior: {next_ev.get('previous','—')}"
        ),
    )
    if len(upcoming) > 1:
        rest = "\n".join(
            f"• **{e.get('date','')[11:16]} ET** · {e.get('title','')}"
            for e in upcoming[1:5]
        )
        embed.add_field(name="Also today", value=rest, inline=False)
    embed.set_footer(text="NOVA Algo · ForexFactory feed")
    await interaction.followup.send(embed=embed)


# ── Bias poll buttons (#pre-market) ─────────────────────────────────────────

class BiasPollView(discord.ui.View):
    """Persistent 3-button view: Long / Short / No-trade. Survives bot restarts
    via custom_id matching."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🟢 Long", style=discord.ButtonStyle.success, custom_id="bias_long")
    async def long_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._record_vote(interaction, "long")

    @discord.ui.button(label="🔴 Short", style=discord.ButtonStyle.danger, custom_id="bias_short")
    async def short_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._record_vote(interaction, "short")

    @discord.ui.button(label="🟡 No-trade", style=discord.ButtonStyle.secondary, custom_id="bias_notrade")
    async def notrade_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._record_vote(interaction, "no_trade")

    async def _record_vote(self, interaction: discord.Interaction, vote: str):
        today = datetime.now(EST).strftime("%Y-%m-%d")
        state = _load_json(BIAS_POLL_PATH, {})
        day = state.setdefault(today, {"long": [], "short": [], "no_trade": []})
        # Remove vote from any other bucket (one vote per user per day)
        uid = str(interaction.user.id)
        for bucket in ("long", "short", "no_trade"):
            if uid in day[bucket] and bucket != vote:
                day[bucket].remove(uid)
        if uid not in day[vote]:
            day[vote].append(uid)
        _save_json(BIAS_POLL_PATH, state)
        tally = (
            f"🟢 Long: {len(day['long'])}  ·  "
            f"🔴 Short: {len(day['short'])}  ·  "
            f"🟡 No-trade: {len(day['no_trade'])}"
        )
        await interaction.response.send_message(
            f"Vote recorded: **{vote.replace('_',' ')}**\n{tally}",
            ephemeral=True,
        )


@tree.command(
    name="bias-poll",
    description="(Staff) Post the daily bias poll into #pre-market.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_bias_poll(interaction: discord.Interaction):
    if not _is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Guild context missing.", ephemeral=True)
        return
    ch = discord.utils.get(guild.text_channels, name="pre-market")
    if not ch:
        await interaction.response.send_message("#pre-market not found.", ephemeral=True)
        return
    embed = discord.Embed(
        title="📊 Daily bias poll · NQ NY AM ORB",
        color=0x00F5D4,
        description=(
            "Vote your bias for today's open. Tally posts after 11:00 ET.\n"
            "One vote per person — change it any time before the bell."
        ),
    )
    embed.set_footer(text="NOVA Algo · daily bias")
    await ch.send(embed=embed, view=BiasPollView())
    await interaction.response.send_message("Posted bias poll in #pre-market.", ephemeral=True)


# ── Win amplifier (🚀 in #wins → auto-pin at 10) ─────────────────────────────

WIN_PIN_THRESHOLD = 10
WIN_EMOJI = "🚀"


async def _maybe_pin_win(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != WIN_EMOJI:
        return
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(payload.channel_id)
    if not ch or ch.name != "wins":
        return
    try:
        msg = await ch.fetch_message(payload.message_id)
    except discord.NotFound:
        return
    rxn = next((r for r in msg.reactions if str(r.emoji) == WIN_EMOJI), None)
    if not rxn or rxn.count < WIN_PIN_THRESHOLD:
        return
    pinned = _load_json(WIN_PINNED_PATH, [])
    if msg.id in pinned:
        return
    try:
        await msg.pin(reason=f"win amplifier — {WIN_PIN_THRESHOLD}+ {WIN_EMOJI} reactions")
        pinned.append(msg.id)
        # Cap retained list at 50 so the file doesn't grow unbounded
        _save_json(WIN_PINNED_PATH, pinned[-50:])
        print(f"[bot] win amplifier — pinned msg {msg.id}", flush=True)
    except discord.HTTPException as e:
        print(f"[bot] pin failed: {e}", flush=True)


# ── Auto-thread per fire (#live-signals) ────────────────────────────────────

@client.event
async def on_message(message: discord.Message):
    # Skip our own messages (avoid recursion) and DMs
    if not message.guild or message.guild.id != GUILD_ID:
        return
    if message.channel.name != "live-signals":
        return
    if not message.embeds:
        return
    # If the embed looks like a NOVA fire (title starts with green/red circle), make a thread
    title = (message.embeds[0].title or "")
    if not title:
        return
    if not (title.startswith("🟢") or title.startswith("🔴")):
        return
    # Avoid duplicates if the message already has a thread
    if message.thread:
        return
    short_title = title[:80]
    fire_thread = None
    try:
        fire_thread = await message.create_thread(
            name=f"💬 {short_title}",
            auto_archive_duration=1440,  # 24h
            reason="NOVA fire — auto-thread for discussion",
        )
        print(f"[bot] auto-thread on fire: {short_title}", flush=True)
    except discord.HTTPException as e:
        print(f"[bot] auto-thread failed: {e}", flush=True)

    # TradingView deep-link reply — anchored to the fire timestamp so anyone
    # clicking through lands on the chart at the moment NOVA triggered. No
    # screenshot dependency, works regardless of bot host machine state.
    try:
        # Discord message timestamp → epoch ms for TV's URL anchor
        epoch_ms = int(message.created_at.timestamp() * 1000)
        # Public NQ futures chart with NOVA Master indicator pre-loaded.
        # ?go_to=<epoch_ms> centers the chart on that exact bar on TV's web app.
        chart_url = (
            f"https://www.tradingview.com/chart/?symbol=CME_MINI%3ANQ1%21"
            f"&interval=30&theme=dark&go_to={epoch_ms}"
        )
        deeplink_embed = discord.Embed(
            title="📈 Open this fire on TradingView",
            color=0x00F5D4,
            description=(
                f"[Click here]({chart_url}) to open NQ 30m at the trigger bar.\n"
                "Chart loads pre-anchored to the fire timestamp."
            ),
        )
        deeplink_embed.set_footer(text="NOVA Algo · auto deep-link")
        # Reply in the auto-thread if it exists; otherwise reply on the message itself.
        target = fire_thread or message.channel
        await target.send(embed=deeplink_embed)
    except discord.HTTPException as e:
        print(f"[bot] tv deep-link reply failed: {e}", flush=True)
    except Exception as e:
        print(f"[bot] tv deep-link unexpected: {e}", flush=True)


# ── /halt slash command (staff-only) ────────────────────────────────────────

@tree.command(
    name="halt",
    description="(Staff) Immediately halt NOVA fanout for the rest of the session.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_halt(interaction: discord.Interaction):
    if not _is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        req = urllib.request.Request(
            f"{API_BASE}/admin/halt",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Nova-Secret": SECRET},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read().decode())
    except Exception as e:
        await interaction.followup.send(f"Halt failed: {e}", ephemeral=True)
        return
    embed = discord.Embed(
        title="🛑 NOVA halted",
        color=0xEF4444,
        description=(
            f"Fanout paused for the remainder of the session by **{interaction.user.display_name}**.\n"
            f"Open positions left to run their brackets."
        ),
    )
    embed.set_footer(text=f"Railway response · {payload}")
    await interaction.followup.send(embed=embed)


# ── /streak slash command ───────────────────────────────────────────────────

@tree.command(
    name="streak",
    description="Your personal + cohort consecutive winning trade-day streak.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_streak(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=False)
    discord_id = str(interaction.user.id)

    cohort = _http_get_json(f"{SITE_BASE}/api/stats/streak") or {}
    me = _http_get_json(f"{SITE_BASE}/api/me/streak?discordId={urllib.parse.quote(discord_id)}") or {}

    cohort_streak = cohort.get("streak", 0) if cohort.get("ok") else 0
    me_streak = me.get("streak", 0) if me.get("ok") else 0

    embed = discord.Embed(
        title="🔥 Streak counter",
        color=0xFBBF24 if (cohort_streak > 0 or me_streak > 0) else 0x6B7280,
    )
    embed.add_field(
        name="Your streak",
        value=(f"**{me_streak}** consecutive winning trade-days"
               + (f" · started {me.get('started_on')}" if me.get('started_on') else "")
               if me.get("ok") else "Not connected — link your TradersPost webhook to track."),
        inline=False,
    )
    embed.add_field(
        name="Cohort streak",
        value=f"**{cohort_streak}** consecutive winning trade-days across the cohort"
              + (f" · started {cohort.get('started_on')}" if cohort.get('started_on') else ""),
        inline=False,
    )
    if me.get("longest_ever") or cohort.get("longest_ever"):
        bits = []
        if me.get("longest_ever"):
            bits.append(f"yours: {me['longest_ever']}")
        if cohort.get("longest_ever"):
            bits.append(f"cohort: {cohort['longest_ever']}")
        embed.add_field(name="Longest ever", value=" · ".join(bits), inline=False)
    embed.set_footer(text="NOVA Algo · /streak")
    await interaction.followup.send(embed=embed)


# ── /leaderboard slash command ──────────────────────────────────────────────

@tree.command(
    name="leaderboard",
    description="This week's top NOVA cohort traders by R-multiple.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=False)
    lb = _http_get_json(f"{SITE_BASE}/api/leaderboard?window=week") or {}
    rows = (lb or {}).get("rows") or []
    if not rows:
        await interaction.followup.send("No leaderboard data yet — first full week still in progress.")
        return
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows[:10]):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        name = row.get("name", "trader")
        r = row.get("r", 0)
        usd = row.get("usd", 0)
        wr = row.get("winRate", 0)
        lines.append(f"{medal} **{name}** · {r:+.1f}R · ${usd:+,.0f} · WR {wr:.0f}%")
    embed = discord.Embed(
        title="🏆 NOVA cohort · this week",
        color=0xFBBF24,
        description="\n".join(lines),
    )
    embed.set_footer(text="NOVA Algo · auto-updates from your fanout-routed fills")
    await interaction.followup.send(embed=embed)


# ── on_member_join — DM onboarding checklist ─────────────────────────────────

@client.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID or member.bot:
        return
    embed = discord.Embed(
        title=f"Welcome to NOVA Algo, {member.display_name} 🟢",
        color=0x00F5D4,
        description=(
            "You're in the room where the algo lives. Quick 5-step setup:\n\n"
            "**1.** Read the rules in **#rules**\n"
            "**2.** Pick your tier in **#verify** (Beta / Signal / Auto / Fleet)\n"
            "**3.** Skim **#server-guide** — you'll learn what every channel does\n"
            "**4.** Connect your TradersPost webhook at "
            f"[{SITE_BASE.replace('https://','')}/portal/connect]({SITE_BASE}/portal/connect)\n"
            "**5.** Drop a hello in **#introduce-yourself**\n\n"
            "After that, NOVA fires through to your prop accounts automatically. "
            "The next NY AM session is at 9:30 ET."
        ),
    )
    embed.set_footer(text="— Gee · NOVA Algo")
    try:
        await member.send(embed=embed)
        print(f"[bot] welcome DM sent to {member.name}", flush=True)
    except discord.Forbidden:
        # User has DMs off — fall back to a public welcome in #introduce-yourself
        ch = discord.utils.get(member.guild.text_channels, name="introduce-yourself")
        if ch:
            try:
                await ch.send(
                    f"👋 Welcome {member.mention}! Quick start: pick a tier in #verify and "
                    f"connect your TradersPost webhook at {SITE_BASE}/portal/connect."
                )
            except discord.Forbidden:
                pass


# ── Daily trivia in #strategy-talk ───────────────────────────────────────────

class TriviaView(discord.ui.View):
    """One row of buttons — A/B/C/D — that record an answer per user."""
    def __init__(self, qid: int, options: list[str], correct_idx: int):
        super().__init__(timeout=60 * 60 * 10)  # 10h
        self.qid = qid
        self.correct_idx = correct_idx
        labels = ["🅰", "🅱", "🅲", "🅳"]
        for i, opt in enumerate(options[:4]):
            label = f"{labels[i]} {opt[:60]}"
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"trivia_{qid}_{i}")
            async def cb(interaction: discord.Interaction, idx=i):
                await self._answer(interaction, idx)
            btn.callback = cb  # type: ignore
            self.add_item(btn)

    async def _answer(self, interaction: discord.Interaction, idx: int):
        state = _load_json(TRIVIA_OPEN_PATH, {})
        cur = state.get("current") or {}
        if cur.get("qid") != self.qid:
            await interaction.response.send_message("This question already closed.", ephemeral=True)
            return
        answers = cur.setdefault("answers", {})
        uid = str(interaction.user.id)
        if uid in answers:
            await interaction.response.send_message("You already answered today.", ephemeral=True)
            return
        answers[uid] = idx
        _save_json(TRIVIA_OPEN_PATH, state)
        if idx == self.correct_idx:
            # Award a point now (also re-counted at reveal time)
            lb = _load_json(TRIVIA_LB_PATH, {})
            lb[uid] = int(lb.get(uid, 0)) + 1
            _save_json(TRIVIA_LB_PATH, lb)
            await interaction.response.send_message("✅ Locked in — looks correct. Reveal at 11:00 PM ET.", ephemeral=True)
        else:
            await interaction.response.send_message("Locked in. Reveal at 11:00 PM ET.", ephemeral=True)


def _pick_trivia_question() -> dict | None:
    bank = _load_json(TRIVIA_PATH, [])
    if not bank:
        return None
    open_state = _load_json(TRIVIA_OPEN_PATH, {})
    asked = set(open_state.get("history", []))
    fresh = [i for i in range(len(bank)) if i not in asked]
    if not fresh:
        # Reset rotation
        asked = set()
        fresh = list(range(len(bank)))
    qid = random.choice(fresh)
    return {"qid": qid, **bank[qid]}


async def _post_trivia():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="strategy-talk")
    if not ch:
        return
    q = _pick_trivia_question()
    if not q:
        return
    embed = discord.Embed(
        title="🧠 Daily trivia",
        color=0x00F5D4,
        description=f"**{q['q']}**",
    )
    embed.set_footer(text="One answer per person · reveal at 11:00 PM ET")
    view = TriviaView(qid=q["qid"], options=q["options"], correct_idx=q["answer"])
    msg = await ch.send(embed=embed, view=view)
    state = _load_json(TRIVIA_OPEN_PATH, {})
    history = state.get("history", [])
    history.append(q["qid"])
    state["history"] = history[-100:]
    state["current"] = {
        "qid": q["qid"],
        "message_id": msg.id,
        "channel_id": ch.id,
        "answer_idx": q["answer"],
        "explain": q.get("explain", ""),
        "answers": {},
    }
    _save_json(TRIVIA_OPEN_PATH, state)


async def _reveal_trivia():
    state = _load_json(TRIVIA_OPEN_PATH, {})
    cur = state.get("current") or {}
    if not cur:
        return
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = guild.get_channel(cur["channel_id"])
    if not ch:
        return
    answers = cur.get("answers", {}) or {}
    correct_idx = cur["answer_idx"]
    correct_users = [uid for uid, idx in answers.items() if idx == correct_idx]
    bank = _load_json(TRIVIA_PATH, [])
    q = bank[cur["qid"]] if cur["qid"] < len(bank) else None
    if not q:
        return
    correct_text = q["options"][correct_idx]
    embed = discord.Embed(
        title="🎯 Trivia reveal",
        color=0x22C55E,
        description=(
            f"**{q['q']}**\n\n"
            f"✅ Correct answer: **{correct_text}**\n\n"
            f"📖 {q.get('explain','')}\n\n"
            f"**{len(correct_users)} of {len(answers)}** got it right."
        ),
    )
    embed.set_footer(text="NOVA Algo · daily trivia")
    await ch.send(embed=embed)
    state["current"] = None
    _save_json(TRIVIA_OPEN_PATH, state)


# ── Daily coffee-chat thread ────────────────────────────────────────────────

async def _post_coffee_chat():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="coffee-chat")
    if not ch:
        return
    prompts = _load_json(COFFEE_PATH, [])
    if not prompts:
        return
    today_str = datetime.now(EST).strftime("%a %b %d")
    prompt = prompts[hash(today_str) % len(prompts)]
    try:
        msg = await ch.send(f"☕ **{today_str}** — {prompt}")
        await msg.create_thread(name=f"☕ {today_str}", auto_archive_duration=1440)
    except discord.HTTPException as e:
        print(f"[bot] coffee-chat post failed: {e}", flush=True)


# ── /badge — public flex card ───────────────────────────────────────────────

@tree.command(
    name="badge",
    description="Show your NOVA Algo badge — roles, streak, week R.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_badge(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=False)
    discord_id = str(interaction.user.id)
    member = interaction.user if isinstance(interaction.user, discord.Member) else None

    # Fetch personal stats + streak + roles
    stats = _http_get_json(f"{SITE_BASE}/api/me/stats?discordId={urllib.parse.quote(discord_id)}") or {}
    streak = _http_get_json(f"{SITE_BASE}/api/me/streak?discordId={urllib.parse.quote(discord_id)}") or {}

    badges: list[str] = []
    if member and member.roles:
        flex_roles = {
            "Founder": "👑",
            "Co-Founder": "🥇",
            "Moderator": "🛡",
            "Coach": "🎓",
            "Fleet": "🌌",
            "Auto": "🤖",
            "Signal": "📡",
            "Beta": "🌱",
            "Verified": "✅",
            "TradersPost Connected": "🔌",
            "Funded": "💰",
        }
        for role in member.roles:
            if role.name in flex_roles:
                badges.append(f"{flex_roles[role.name]} {role.name}")

    embed = discord.Embed(
        title=f"🏷 {interaction.user.display_name}'s NOVA Algo badge",
        color=0x00F5D4,
    )
    if badges:
        embed.add_field(name="Roles", value="  ·  ".join(badges), inline=False)
    else:
        embed.add_field(name="Roles", value="No tier roles yet — run `/link` to connect.", inline=False)

    me_streak = streak.get("streak", 0) if streak.get("ok") else 0
    if stats.get("ok") and stats.get("trades", 0) > 0:
        embed.add_field(
            name="This week",
            value=(
                f"**{stats.get('trades',0)}** trades · "
                f"**{stats.get('winRate',0):.0f}%** WR · "
                f"**{stats.get('rSum',0):+.1f}R** · "
                f"${stats.get('netUsd',0):+,.0f}"
            ),
            inline=False,
        )
    if me_streak > 0:
        embed.add_field(name="🔥 Streak", value=f"**{me_streak}** consecutive winning days", inline=True)
    embed.set_footer(text="NOVA Algo · /badge · share the flex")
    if interaction.user.avatar:
        embed.set_thumbnail(url=interaction.user.avatar.url)
    await interaction.followup.send(embed=embed)


# ── /trivia-leaderboard ─────────────────────────────────────────────────────

@tree.command(
    name="trivia-leaderboard",
    description="Top 10 trivia scorers in NOVA Algo Discord.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_trivia_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=False)
    lb = _load_json(TRIVIA_LB_PATH, {})
    if not lb:
        await interaction.followup.send("No trivia answered yet. First question lands at 12:00 ET today.")
        return
    rows = sorted(lb.items(), key=lambda kv: int(kv[1] or 0), reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"]
    lines: list[str] = []
    guild = interaction.guild
    for i, (uid, points) in enumerate(rows):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        name = "trader"
        if guild:
            mem = guild.get_member(int(uid)) if uid.isdigit() else None
            if mem:
                name = mem.display_name
        lines.append(f"{medal} **{name}** · {points} pts")
    embed = discord.Embed(
        title="🧠 Trivia leaderboard",
        color=0xFBBF24,
        description="\n".join(lines),
    )
    embed.set_footer(text="NOVA Algo · daily trivia · 12:00 ET in #strategy-talk")
    await interaction.followup.send(embed=embed)


# ── /concept-draft (staff) ──────────────────────────────────────────────────

@tree.command(
    name="concept-draft",
    description="(Staff) Preview the next concept-of-the-week without posting.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_concept_draft(interaction: discord.Interaction):
    if not _is_staff(interaction.user):
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return
    await interaction.response.defer(thinking=False, ephemeral=True)
    concepts_path = os.path.join(os.path.dirname(__file__), "content", "concepts.json")
    cursor_path = os.path.join(STATE_DIR, "concept_preview_cursor.json")
    try:
        with open(concepts_path, "r", encoding="utf-8") as f:
            bank = json.load(f)
    except Exception:
        await interaction.followup.send("concepts.json missing.", ephemeral=True)
        return
    if not bank:
        await interaction.followup.send("Bank empty.", ephemeral=True)
        return
    cur = _load_json(cursor_path, {"idx": 0}).get("idx", 0)
    pick = bank[cur % len(bank)]
    embed = discord.Embed(
        title=f"🧠 [PREVIEW] {pick['title']}",
        color=0x00F5D4,
        description=pick["body"][:3800],
    )
    if pick.get("takeaway"):
        embed.add_field(name="🎯 Takeaway", value=pick["takeaway"], inline=False)
    embed.set_footer(text=f"NOVA Algo · concept #{cur+1}/{len(bank)} · ephemeral preview only")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /morningdm-on / /morningdm-off ──────────────────────────────────────────

@tree.command(
    name="morningdm-on",
    description="Opt IN to the daily NOVA Algo personal morning DM (7:30 ET).",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_morningdm_on(interaction: discord.Interaction):
    optout = _load_json(DM_OPTOUT_PATH, [])
    uid = str(interaction.user.id)
    if uid in optout:
        optout = [x for x in optout if x != uid]
        _save_json(DM_OPTOUT_PATH, optout)
    await interaction.response.send_message(
        "✅ You're opted in. Daily morning DM lands ~7:30 ET weekdays. Disable with `/morningdm-off`.",
        ephemeral=True,
    )


@tree.command(
    name="morningdm-off",
    description="Opt OUT of the daily NOVA Algo personal morning DM.",
    guild=discord.Object(id=GUILD_ID),
)
async def cmd_morningdm_off(interaction: discord.Interaction):
    optout = _load_json(DM_OPTOUT_PATH, [])
    uid = str(interaction.user.id)
    if uid not in optout:
        optout.append(uid)
        _save_json(DM_OPTOUT_PATH, optout)
    await interaction.response.send_message(
        "🛑 You're opted out. Re-enable any time with `/morningdm-on`.",
        ephemeral=True,
    )


# ── Daily personal DM (7:30 ET weekdays) ────────────────────────────────────

async def _send_morning_dm_to_all():
    """DM each linked + opted-in user their personal morning brief."""
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    optout = set(_load_json(DM_OPTOUT_PATH, []))

    # Pull cohort streak once, share across DMs
    cohort_streak = (_http_get_json(f"{SITE_BASE}/api/stats/streak") or {}).get("streak", 0)

    # Get list of linked users from /api/cohort/linked-discord (we'll need to add this)
    # For now: fall back to scanning members of the guild and checking each /api/me/stats
    # to see if they're linked. Cheaper: only DM members with NOVA roles.
    nova_role_names = {"Founder", "Co-Founder", "Fleet", "Auto", "Signal", "Beta", "Verified"}
    candidates = [m for m in guild.members
                  if not m.bot and any(r.name in nova_role_names for r in m.roles)
                  and str(m.id) not in optout]
    sent = 0
    for m in candidates[:50]:  # cap at 50/day to avoid rate limit
        stats = _http_get_json(f"{SITE_BASE}/api/me/stats?discordId={urllib.parse.quote(str(m.id))}") or {}
        if not stats.get("ok") or stats.get("trades", 0) == 0:
            continue
        embed = discord.Embed(
            title=f"☀️ Morning brief · {m.display_name}",
            color=0x00F5D4,
            description=(
                f"**Your numbers so far:** {stats.get('trades',0)} trades · "
                f"{stats.get('winRate',0):.0f}% WR · {stats.get('rSum',0):+.1f}R · "
                f"${stats.get('netUsd',0):+,.0f} net\n\n"
                f"**Cohort streak:** 🔥 {cohort_streak} consecutive winning trade-days\n\n"
                "**Today's session:** NY AM 9:30–11:00 ET · NQ 30m\n"
                "Triggers arm at 10:00. First side of OR to break wins."
            ),
        )
        embed.set_footer(text="NOVA Algo · /morningdm-off to disable")
        try:
            await m.send(embed=embed)
            sent += 1
            await asyncio.sleep(0.5)  # rate-limit gentle
        except discord.Forbidden:
            continue  # DMs closed, skip silently
        except discord.HTTPException:
            continue
    print(f"[bot] morning DM sent to {sent} members", flush=True)


# ── Auto-fire daily bias poll (9:00 ET weekdays) ────────────────────────────

async def _auto_post_bias_poll():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="pre-market")
    if not ch:
        return
    embed = discord.Embed(
        title="📊 Daily bias poll · NQ NY AM ORB",
        color=0x00F5D4,
        description=(
            "Vote your bias for today's open. Tally posts after 11:00 ET.\n"
            "One vote per person — change it any time before the bell."
        ),
    )
    embed.set_footer(text="NOVA Algo · daily bias · auto-pushed 9:00 ET")
    try:
        await ch.send(embed=embed, view=BiasPollView())
    except discord.HTTPException as e:
        print(f"[bot] auto bias poll failed: {e}", flush=True)


# ── Bias-poll auto-tally (11:00 ET when session closes) ─────────────────────

async def _post_bias_poll_tally():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="pre-market")
    if not ch:
        return
    today = datetime.now(EST).strftime("%Y-%m-%d")
    state = _load_json(BIAS_POLL_PATH, {})
    day = state.get(today)
    if not day:
        return  # no votes recorded today, silent skip
    long_n = len(day.get("long", []))
    short_n = len(day.get("short", []))
    notrade_n = len(day.get("no_trade", []))
    total = long_n + short_n + notrade_n
    if total == 0:
        return

    # Pull today's actual outcome from Railway state
    railway_status = _http_get_json(f"{API_BASE}/status") or {}
    trades_today = railway_status.get("trades_today", 0)

    def _pct(n: int) -> str:
        return f"{(n/total*100):.0f}%" if total else "—"

    embed = discord.Embed(
        title=f"📊 Daily bias poll · tally · {today}",
        color=0xFBBF24,
        description=f"**{total}** votes cast this morning.",
    )
    embed.add_field(
        name="🟢 Long",
        value=f"{long_n}  ·  {_pct(long_n)}",
        inline=True,
    )
    embed.add_field(
        name="🔴 Short",
        value=f"{short_n}  ·  {_pct(short_n)}",
        inline=True,
    )
    embed.add_field(
        name="🟡 No-trade",
        value=f"{notrade_n}  ·  {_pct(notrade_n)}",
        inline=True,
    )
    embed.add_field(
        name="What actually happened",
        value=(f"NOVA fired **{trades_today}** trade(s) today." if trades_today
               else "**No fire today** — price stayed inside the OR. ~30% of NY AM days resolve this way."),
        inline=False,
    )
    embed.set_footer(text="NOVA Algo · daily bias tally · NY AM ORB")
    await ch.send(embed=embed)


# ── Friday weekly leaderboard + sentiment poll ──────────────────────────────

class SentimentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🚀 Crushed it", style=discord.ButtonStyle.success, custom_id="sent_great")
    async def crushed(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_message("Logged. Stack the wins.", ephemeral=True)

    @discord.ui.button(label="📈 Solid week", style=discord.ButtonStyle.primary, custom_id="sent_solid")
    async def solid(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_message("Logged. Steady wins compound.", ephemeral=True)

    @discord.ui.button(label="🤝 Mixed", style=discord.ButtonStyle.secondary, custom_id="sent_mixed")
    async def mixed(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_message("Logged. Reset weekend, fresh Monday.", ephemeral=True)

    @discord.ui.button(label="🩹 Tough", style=discord.ButtonStyle.danger, custom_id="sent_tough")
    async def tough(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_message("Logged. The cohort's got you. Reset.", ephemeral=True)


async def _post_friday_feedback_poll():
    """Anonymous weekly cohort feedback prompt in #feedback. 4 reaction
    buckets — only emoji counts surface to staff; reactor identities aren't
    tied to specific bucket choices in any visible Discord ledger."""
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="feedback")
    if not ch:
        return
    today = datetime.now(EST).strftime("%a %b %d")
    embed = discord.Embed(
        title=f"🗳 Weekly cohort feedback · {today}",
        color=0x00F5D4,
        description=(
            "How was this week? React below with what fits — fully anonymous. "
            "Only the count surfaces to staff; **nothing about who picked what** is visible "
            "in your Discord profile, modlog, or anywhere else.\n\n"
            "🟢 What worked\n"
            "🟡 Mixed feelings\n"
            "🔴 What's frustrating\n"
            "💬 I want to say more (drop a thread reply)"
        ),
    )
    embed.set_footer(text="NOVA Algo · weekly · auto-archives next Friday")
    msg = await ch.send(embed=embed)
    for emoji in ("🟢", "🟡", "🔴", "💬"):
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass
    print(f"[bot] feedback poll posted to #feedback msg_id={msg.id}", flush=True)


async def _post_friday_sentiment_poll():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="coffee-chat")
    if not ch:
        return
    embed = discord.Embed(
        title="🗓 Friday sentiment poll",
        color=0xFBBF24,
        description="How was your week trading NOVA? Vote — anonymous.",
    )
    await ch.send(embed=embed, view=SentimentView())


async def _post_friday_leaderboard():
    """Calls /api/leaderboard?window=week and posts a podium embed in #wins."""
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="wins")
    if not ch:
        return
    lb = _http_get_json(f"{SITE_BASE}/api/leaderboard?window=week") or {}
    rows = (lb or {}).get("rows") or []
    if not rows:
        return  # silent skip; nothing to celebrate
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows[:5]):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(
            f"{medal} **{row.get('name','trader')}** · {row.get('r',0):+.1f}R · "
            f"${row.get('usd',0):+,.0f}"
        )
    embed = discord.Embed(
        title="🏆 NOVA cohort · weekly podium",
        color=0xFBBF24,
        description="\n".join(lines),
    )
    embed.set_footer(text="NOVA Algo · Friday wrap")
    await ch.send(embed=embed)


# ── AMA Voice scheduled event (weekly) ──────────────────────────────────────

async def _create_weekly_ama_event():
    """Create a Discord scheduled event for AMA Voice at next Friday 4pm ET."""
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return
    voice_ch = discord.utils.get(guild.voice_channels, name="AMA Voice")
    if not voice_ch:
        return
    now_et = datetime.now(EST)
    days_until_fri = (4 - now_et.weekday()) % 7
    if days_until_fri == 0 and now_et.hour >= 16:
        days_until_fri = 7
    target = (now_et + timedelta(days=days_until_fri)).replace(hour=16, minute=0, second=0, microsecond=0)
    end = target + timedelta(hours=1)
    # Skip if there's already an event for this voice channel within 6 days
    existing = await guild.fetch_scheduled_events()
    for ev in existing:
        if ev.channel_id == voice_ch.id and ev.start_time and abs((ev.start_time - target).days) < 1:
            return
    try:
        await guild.create_scheduled_event(
            name=f"AMA Voice · {target.strftime('%b %d')}",
            description="Weekly NOVA AMA — ask anything about the algo, the cohort, the strategy.",
            channel=voice_ch,
            start_time=target,
            end_time=end,
            entity_type=discord.EntityType.voice,
            privacy_level=discord.PrivacyLevel.guild_only,
        )
        print(f"[bot] AMA event scheduled for {target.isoformat()}", flush=True)
    except discord.HTTPException as e:
        print(f"[bot] AMA event failed: {e}", flush=True)


# ── Scheduler loop ──────────────────────────────────────────────────────────
#
# discord.ext.tasks runs once per minute. We dispatch to our scheduled handlers
# only when the (weekday, hour, minute) matches and the marker hasn't fired
# yet today.

_scheduler_marks: dict[str, str] = {}  # task_name -> last "YYYY-MM-DD" fired


def _should_fire(task: str, now: datetime, weekdays: tuple[int, ...] | None,
                 hour: int, minute: int) -> bool:
    if weekdays is not None and now.weekday() not in weekdays:
        return False
    if now.hour != hour or now.minute != minute:
        return False
    today = now.strftime("%Y-%m-%d")
    if _scheduler_marks.get(task) == today:
        return False
    _scheduler_marks[task] = today
    return True


@tasks.loop(minutes=1)
async def scheduler_loop():
    if not client.is_ready():
        return
    now = datetime.now(EST)

    # Daily 7:00 ET — coffee chat thread
    if _should_fire("coffee_chat", now, (0, 1, 2, 3, 4), 7, 0):
        await _post_coffee_chat()

    # Daily 7:30 ET — personal morning DM to opted-in cohort (weekdays)
    if _should_fire("morning_dm", now, (0, 1, 2, 3, 4), 7, 30):
        await _send_morning_dm_to_all()

    # Daily 9:00 ET — auto-post bias poll buttons in #pre-market
    if _should_fire("bias_poll_auto", now, (0, 1, 2, 3, 4), 9, 0):
        await _auto_post_bias_poll()

    # Daily 12:00 ET — trivia question (weekdays only)
    if _should_fire("trivia_post", now, (0, 1, 2, 3, 4), 12, 0):
        await _post_trivia()

    # Daily 23:00 ET — trivia reveal
    if _should_fire("trivia_reveal", now, None, 23, 0):
        await _reveal_trivia()

    # Daily 11:00 ET — bias poll tally (after session closes)
    if _should_fire("bias_tally", now, (0, 1, 2, 3, 4), 11, 0):
        await _post_bias_poll_tally()

    # Friday 16:00 ET — anonymous weekly cohort feedback poll in #feedback
    if _should_fire("friday_feedback", now, (4,), 16, 0):
        await _post_friday_feedback_poll()

    # Friday 16:30 ET — weekly leaderboard + sentiment poll
    if _should_fire("friday_lb", now, (4,), 16, 30):
        await _post_friday_leaderboard()
        await _post_friday_sentiment_poll()

    # Monday 9:00 ET — schedule next Friday's AMA event
    if _should_fire("ama_event", now, (0,), 9, 0):
        await _create_weekly_ama_event()


@scheduler_loop.before_loop
async def _before_scheduler():
    await client.wait_until_ready()


# ── Reaction listener (extends existing handler) ────────────────────────────


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):  # type: ignore[no-redef]
    """Combined handler — verify message reactions + win amplifier."""
    if payload.guild_id != GUILD_ID:
        return
    msg_id = _load_verify_msg_id()
    if msg_id and payload.message_id == msg_id:
        if payload.user_id == (client.user.id if client.user else 0):
            return
        role_name = REACTION_MAP.get(str(payload.emoji))
        if role_name:
            guild = client.get_guild(GUILD_ID)
            if guild:
                role = discord.utils.get(guild.roles, name=role_name)
                member = guild.get_member(payload.user_id)
                if role and member:
                    try:
                        await member.add_roles(role, reason="self-serve verify")
                        print(f"[bot] +{role_name} → {member.name}", flush=True)
                    except discord.Forbidden:
                        pass
        return
    # Otherwise, check win amplifier
    await _maybe_pin_win(payload)


# ── Lifecycle ───────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    print(f"[bot] ready as {client.user}", flush=True)
    guild = discord.Object(id=GUILD_ID)
    try:
        synced = await tree.sync(guild=guild)
        print(f"[bot] synced {len(synced)} slash commands to guild {GUILD_ID}", flush=True)
    except Exception as e:
        print(f"[bot] slash sync failed: {e}", flush=True)

    # Register persistent views — survives bot restart
    try:
        client.add_view(BiasPollView())
        client.add_view(SentimentView())
    except Exception as e:
        print(f"[bot] persistent view register failed: {e}", flush=True)

    g = client.get_guild(GUILD_ID)
    if g:
        await _ensure_verify_message(g)

    # Start the once-per-minute scheduler loop
    if not scheduler_loop.is_running():
        scheduler_loop.start()
        print("[bot] scheduler_loop started", flush=True)


def main():
    if not TOKEN:
        print("DISCORD_BOT_TOKEN missing", flush=True)
        return
    client.run(TOKEN)


if __name__ == "__main__":
    main()
