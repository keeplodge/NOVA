"""
populate_channels.py — seed every empty NOVA Algo Discord channel with
starter content (explainers, ICT primers, strategy deep-dives, calculators).

Idempotent: any channel that already has a message is skipped, so this is
safe to re-run when new channels get added or content gets revised.

Run after setup.py + post_setup.py have provisioned the server.
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

NOVA_CYAN  = Color(0x00F5D4)
NOVA_GREEN = Color(0x22C55E)
NOVA_RED   = Color(0xEF4444)
NOVA_AMBER = Color(0xF59E0B)
NOVA_BLUE  = Color(0x3B82F6)
NOVA_PURPLE = Color(0xA855F7)

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)


# ── Channel content map ──────────────────────────────────────────────────────
# Each entry: channel_name → list of embeds to post (in order).
# Embeds are dicts with title/description/color/fields.

def E(title, description=None, color=NOVA_CYAN, fields=None, footer=None):
    embed = discord.Embed(title=title, color=color)
    if description:
        embed.description = description
    if fields:
        for f in fields:
            embed.add_field(
                name=f["name"], value=f["value"], inline=f.get("inline", False),
            )
    if footer:
        embed.set_footer(text=footer)
    return embed


CONTENT: dict[str, list[discord.Embed]] = {

    # ── START HERE ───────────────────────────────────────────────────────────
    "introduce-yourself": [
        E(
            "👋 New here? Drop a hello.",
            "Three lines, doesn't need to be polished:\n\n"
            "**1. Where you trade from** (Toronto, NYC, London, etc.)\n"
            "**2. How long you've been trading** (and what — futures, options, FX, crypto)\n"
            "**3. What brought you to NOVA Algo** (referral, twitter, podcast, the algo itself)\n\n"
            "Bonus: drop a screenshot of your TradingView setup or your prop firm dashboard. Always a vibe.",
            footer="No gatekeepers. No judgment. Just traders.",
        ),
    ],

    # ── ANNOUNCEMENTS ────────────────────────────────────────────────────────
    "changelog": [
        E(
            "📝 NOVA Algo — Changelog",
            "Every code, strategy, and infra update lands here. Read-only.\n\n"
            "Subscribers: this channel is your audit trail. If you ever ask "
            "*\"what changed and when?\"* — the answer is here.",
            footer="Auto-posted by Railway when commits land on main.",
        ),
        E(
            "v11.0 — Live forward-test (2026-04-20)",
            "Initial live deployment of NOVA NQ ORB strategy.",
            color=NOVA_GREEN,
            fields=[
                {"name": "Risk model", "value": "$500 SL / $1500 TP (3R) · BE at 1R · trailing 1R after BE"},
                {"name": "Sessions",   "value": "NY AM (8:30am–11am ET) — primary execution window"},
                {"name": "Instrument", "value": "NQ futures, 15m chart"},
                {"name": "Routing",    "value": "TradingView → Railway → TradersPost → prop/live accounts"},
                {"name": "Cumulative stats", "value": "241 trades · 53.5% WR · 1.4 PF · +0.19R expectancy (Nov 2025 → Apr 2026)"},
            ],
            footer="commit 040e248 · live since 2026-04-20",
        ),
    ],

    "roadmap": [
        E(
            "🛣️ NOVA Algo — Roadmap",
            "Where we're going, what's shipping next, what's on hold.\n\n"
            "Updated when major decisions get made. Not a precise calendar — direction.",
            color=NOVA_CYAN,
        ),
        E(
            "Now → Next 2 weeks",
            None,
            fields=[
                {"name": "✅ Beta launch (15 free seats)", "value": "Live now. Onboarding via #onboarding-tradespost."},
                {"name": "🚧 Auto tier ($297/mo) — public", "value": "Stripe wired, gating on first 5 paying betas converting first."},
                {"name": "🚧 Discord auto-posting (signals/equity)", "value": "Built; ships post-NY-AM-close 2026-04-27."},
                {"name": "🚧 Stats dashboard live in Discord", "value": "Pulls from Railway. ETA this week."},
            ],
        ),
        E(
            "Next 30 days",
            None,
            fields=[
                {"name": "Fleet tier ($997/mo)", "value": "5-account fanout, dedicated onboarding."},
                {"name": "Mobile push notifications", "value": "Telegram or native iOS, leaning Telegram for speed."},
                {"name": "Affiliate program", "value": "30% rev-share for the first cohort of referrers."},
            ],
        ),
        E(
            "Q3 2026 +",
            None,
            fields=[
                {"name": "Second strategy", "value": "London ORB on NQ — backtested, awaiting forward soak."},
                {"name": "Additional instruments", "value": "ES futures consideration. No XAUUSD, no crypto."},
                {"name": "Closed-source strategy IP", "value": "Public-facing only the entries/exits, never the logic."},
            ],
            footer="Opinions on what to prioritize? Drop them in #suggestions.",
        ),
    ],

    "status": [
        E(
            "🚦 System status",
            "Real-time uptime + incident log for the NOVA Algo trading stack.\n\n"
            "**Components monitored:**\n"
            "• TradingView alert dispatcher\n"
            "• Railway server (`/webhook` endpoint)\n"
            "• TradersPost routing layer\n"
            "• Subscriber fanout pipeline\n"
            "• Vercel marketing site (novaalgo.org)\n\n"
            "Auto-posts here when anything degrades. Otherwise: silent = healthy.",
            color=NOVA_GREEN,
            footer="Live status: https://nova-production-72f5.up.railway.app/status",
        ),
    ],

    "milestones": [
        E(
            "🎉 Milestones",
            "Account flips, beta seat fills, big wins, member achievements.\n\n"
            "Auto-posted from Railway when notable events fire. Members can also "
            "tag a mod to get a milestone post pinned.",
            color=NOVA_AMBER,
        ),
        E(
            "🚀 Beta launch — Day 1",
            "**2026-04-27** — Beta cohort opened. 15 free seats forever.\n\n"
            "First subscribers in the door. NY AM session is live as the launch fires.",
            color=NOVA_GREEN,
            footer="Launch day · Mon Apr 27, 2026",
        ),
    ],

    # ── LIVE TRADING ─────────────────────────────────────────────────────────
    "morning-brief": [
        E(
            "🌅 Morning Brief — what shows up here",
            "Every weekday at **8:00am ET**, this channel auto-populates with:\n\n"
            "• **Bias** — long, short, or neutral, based on weekly opens + PDH/PDL relationships\n"
            "• **Key levels** — PDH, PDL, weekly open, Asia high/low, London high/low\n"
            "• **Conditions** — clean tape vs choppy, news-driven vs technical\n"
            "• **Risk events** — CPI, NFP, FOMC, anything that could disrupt the open\n\n"
            "Read this before the 8:30am NY AM bell. If you're going to take any discretion "
            "around the algo's signals, this is the context to take it on.",
            footer="Auto-posted from Railway. Don't expect this on weekends or holidays.",
        ),
    ],

    "pre-market": [
        E(
            "🌒 Pre-market",
            "Early-morning notes from before the 8:30am bell.\n\n"
            "**What lands here:**\n"
            "• Key levels carried over from Asia/London sessions\n"
            "• Overnight news that could move NQ\n"
            "• Pre-market gap analysis (above/below PDH/PDL)\n"
            "• Anything moving European indices that bleeds into US futures\n\n"
            "Lighter than #morning-brief — this is *additional* context, not a replacement.",
        ),
    ],

    "session-open": [
        E(
            "🔔 Session open — NY AM",
            "**8:30am ET — the bell that matters for NOVA Algo.**\n\n"
            "This channel marks session kickoff and tracks the **Opening Range** (first 15m).\n\n"
            "**What you'll see here daily:**\n"
            "• Range high + range low (locked at 8:45am)\n"
            "• Range size in points (signals quality of setup ahead)\n"
            "• Liquidity sweeps off the range edges\n"
            "• Mark-up to #live-signals when NOVA fires\n\n"
            "Quiet days: empty. Active days: noisy.",
            footer="The range is the signal. The breakout is the trade.",
        ),
    ],

    "trade-journal": [
        E(
            "📔 Auto-posted trade journal",
            "Every fired NOVA Algo trade gets a post-mortem here within minutes of the close.\n\n"
            "**Format:**\n"
            "• Entry / SL / TP and outcome (W / L / BE)\n"
            "• Why the setup fired (which liquidity sweep, which session)\n"
            "• Screenshot of the chart at execution time\n"
            "• Lesson or pattern note if applicable\n\n"
            "Use this channel to learn the strategy *as it runs*. Watching 50 of these "
            "back-to-back is better than any course on ICT futures trading.",
            footer="Auto-generated by the NOVA Analyst Agent post-trade.",
        ),
    ],

    "equity-curve": [
        E(
            "📈 Daily equity snapshots",
            "Every trading day after the close, this channel posts the **founder fleet's** "
            "live equity across all eval/funded accounts.\n\n"
            "**Why we publish this:** because everyone in trading lies about returns. "
            "Receipts > vibes. If you're paying for signals, you should see what they "
            "actually do for the founder running them on real capital.\n\n"
            "**What gets posted:**\n"
            "• Each account's current equity vs target\n"
            "• Day's P&L\n"
            "• Progress bar toward eval payout\n"
            "• Total fleet equity",
            color=NOVA_GREEN,
            footer="Read-only. Auto-posted by Railway each weekday after close.",
        ),
    ],

    "key-levels": [
        E(
            "🎯 Key levels — daily",
            "Posted every morning before 8am ET. The structural levels NOVA cares about.\n\n"
            "**Standard daily set:**\n"
            "• **PDH / PDL** — previous day high / low\n"
            "• **Weekly open** — Sunday 6pm ET reference\n"
            "• **Monthly open** — first trading day reference\n"
            "• **Asia / London ranges** — overnight session boundaries\n"
            "• **Equilibrium** — midpoint of yesterday's range\n\n"
            "These aren't predictions. They're the pricing landmarks the algo respects "
            "when it sequences a setup. Liquidity rests at these levels.",
            footer="Levels carry across sessions. Plan your day around them.",
        ),
    ],

    "news-feed": [
        E(
            "🗞️ News & macro",
            "Auto-posted high-impact macro events that could disrupt NQ:\n\n"
            "**Gets posted:**\n"
            "• FOMC rate decisions + minutes\n"
            "• CPI / PCE / PPI inflation prints\n"
            "• NFP (non-farm payrolls)\n"
            "• Powell speeches (live)\n"
            "• Sudden geopolitical breaks (Iran, China, etc.)\n\n"
            "**Doesn't get posted:** the noise. Twitter takes, sell-side notes, "
            "earnings of single stocks.\n\n"
            "If something's posted here, you should know about it before NY AM opens.",
        ),
    ],

    # ── EDUCATION ────────────────────────────────────────────────────────────
    "ict-fundamentals": [
        E(
            "📘 ICT Fundamentals — start here",
            "**Inner Circle Trader (ICT)** is the framework NOVA Algo runs on.\n\n"
            "It's not technical analysis as you've seen it. No moving averages. No RSI. "
            "No Fibonacci spaghetti. Just **how price actually moves** — driven by liquidity, "
            "displacement, and time.\n\n"
            "Below: 5 concepts you need cold. Read top to bottom — each builds on the last.",
            color=NOVA_PURPLE,
        ),
        E(
            "1. Liquidity",
            "Price doesn't move randomly. It moves to **liquidity** — pools of resting orders.\n\n"
            "**Where liquidity sits:**\n"
            "• Above swing highs (buy-stops from short-sellers)\n"
            "• Below swing lows (sell-stops from long-buyers)\n"
            "• Above/below session opens, daily/weekly opens\n"
            "• Above PDH (previous day high), below PDL\n\n"
            "**The take:** market makers and algos *target* these pools to fill their own orders. "
            "What looks like a \"breakout\" is often price grabbing that liquidity, then reversing.",
            footer="If you can't see liquidity, you're trading blind.",
        ),
        E(
            "2. Sweep / Stop-hunt",
            "A **sweep** is when price quickly takes out a liquidity pool and then reverses.\n\n"
            "**Recognize it:**\n"
            "• Price spikes through a clear high (or low) on increased velocity\n"
            "• Then **rejects** within 1-3 candles\n"
            "• The wick is *much longer* than typical bars\n\n"
            "**The take:** sweeps are NOVA's preferred entry trigger. They show the smart side "
            "is being filled. The dumb side just got stopped out. Now follow the smart side.",
            footer="Sweep above PDH → expect short. Sweep below PDL → expect long.",
        ),
        E(
            "3. MSS — Market Structure Shift",
            "A **shift** in market structure is when price *breaks* a recent high/low in the "
            "opposite direction of the prevailing trend.\n\n"
            "**Bullish MSS:** price had been making lower-lows and lower-highs. Then it "
            "**breaks above** the most recent lower-high. That's a shift. Bias just flipped long.\n\n"
            "**Bearish MSS:** opposite — was making higher-highs/higher-lows, now breaks the "
            "most recent higher-low. Bias flipped short.\n\n"
            "**The take:** MSS is the confirmation. Sweep identifies *where* to look. MSS confirms "
            "the reversal is real. NOVA waits for both before firing.",
        ),
        E(
            "4. FVG — Fair Value Gap",
            "A **FVG** is a 3-candle imbalance where price moved so fast it left a gap.\n\n"
            "**Recognize it:**\n"
            "• Candle 1 has a high\n"
            "• Candle 3 has a low *higher* than candle 1's high (or vice-versa for bear FVG)\n"
            "• The space between them is the imbalance\n\n"
            "**The take:** price often comes back to fill that gap. FVGs are NOVA's preferred "
            "entry zones — fade the retracement back into them, then ride the displacement.",
            footer="FVG = inefficiency. Markets seek efficiency.",
        ),
        E(
            "5. OB — Order Block",
            "An **order block** is the last candle before a big displacement move.\n\n"
            "**Bullish OB:** the last *down* candle before a sharp rally. That's where institutions "
            "loaded longs.\n\n"
            "**Bearish OB:** the last *up* candle before a sharp drop. That's where institutions "
            "loaded shorts.\n\n"
            "**The take:** when price returns to that candle, it often respects the level. "
            "OBs combined with FVGs and sweeps form NOVA's full setup grammar.",
            footer="Master these 5 and you can read any chart. Skip them and you're guessing.",
        ),
    ],

    "strategy-deep-dive": [
        E(
            "🧠 NOVA Algo — Strategy Deep Dive",
            "**The full breakdown of what NOVA actually does and why.**\n\n"
            "If you're going to trust the algo with real money, read this end-to-end.",
            color=NOVA_PURPLE,
        ),
        E(
            "1. The setup — NY AM Opening Range",
            "NOVA trades the **NY AM session: 8:30am–11:00am ET**.\n\n"
            "The **Opening Range (OR)** is the high-low band of the first 15 minutes (8:30–8:45am).\n\n"
            "Why this window:\n"
            "• Highest volume window of the US session\n"
            "• Cash market opens at 9:30am, futures lead — best liquidity for ORB strategies\n"
            "• News risk is largely absorbed by 8:30 (jobs/CPI prints at 8:30 ET sharp)\n"
            "• Range completes by 8:45am, leaving 2h 15m of trade window",
        ),
        E(
            "2. The trigger — sweep + MSS",
            "NOVA only fires when it sees:\n\n"
            "**a) Liquidity sweep** of the OR high or low\n"
            "    → price wicks through the range edge and rejects\n\n"
            "**b) Market Structure Shift (MSS)** in the opposite direction\n"
            "    → confirms the sweep was a reversal, not continuation\n\n"
            "**c) Bias alignment**\n"
            "    → if PDH/PDL/weekly opens point one way, NOVA only takes setups in that direction\n\n"
            "Without all three: no signal. False breakouts get filtered out.",
        ),
        E(
            "3. The risk model",
            None,
            fields=[
                {"name": "Stop loss", "value": "**$500/contract** — tight, defined risk per signal"},
                {"name": "Take profit", "value": "**$1500/contract** — 3:1 reward-to-risk"},
                {"name": "Breakeven", "value": "Trail to breakeven at 1R (50% of TP)"},
                {"name": "Trail", "value": "After BE, trail 1R behind price to lock gains"},
                {"name": "Position size", "value": "Max 2 contracts per signal (configurable per subscriber)"},
                {"name": "Daily loss cap", "value": "$500 — locks Railway after second losing trade"},
                {"name": "Daily trade cap", "value": "5 max — prevents revenge trading"},
            ],
        ),
        E(
            "4. Why this works — the edge",
            "**Edge sources, ranked:**\n\n"
            "**1. Time-of-day liquidity** — NY AM has the deepest book. Fills are crisp, slippage minimal.\n\n"
            "**2. Range failures vs successes** — backtest shows ~58% of NY AM range breakouts fail "
            "and reverse. NOVA enters on the *failure* (sweep-and-revert), which is the higher-probability play.\n\n"
            "**3. ICT structural confluence** — sweeps + MSS + bias alignment is rare enough to "
            "create selection bias. Most signals get filtered. The ones that fire are pre-graded.\n\n"
            "**4. 3R reward-to-risk** — even at 35% win rate this is profitable. At 53.5% (current live), "
            "the expectancy is +0.19R per trade — meaning every signal averages $95 profit on a $500 stop.",
        ),
        E(
            "5. What NOVA does NOT do",
            "Just as important — what's *out of scope*:\n\n"
            "• ❌ **No martingale, no averaging down.** One stop, walk away.\n"
            "• ❌ **No revenge trades.** Daily caps enforced at the Railway gate.\n"
            "• ❌ **No discretionary overrides.** Algo fires, algo gets routed.\n"
            "• ❌ **No XAUUSD, no crypto.** NQ futures only. Best liquidity, best fills, no overnight gap risk.\n"
            "• ❌ **No FOMC trades.** When Powell speaks, NOVA stands down. Tested — degrades edge.\n"
            "• ❌ **No earnings-week single stocks.** Not what we trade. NQ futures only.",
            footer="The strategy is narrow on purpose. Narrow = repeatable.",
        ),
    ],

    "video-lessons": [
        E(
            "🎥 Video lessons — coming",
            "Recorded walkthroughs of:\n\n"
            "• Setting up TradingView for NOVA's ICT charts\n"
            "• Reading liquidity in real-time on NQ\n"
            "• How to journal trades effectively\n"
            "• Prop firm strategy — passing evals with NOVA\n\n"
            "**Status:** filming. First lesson drops within 7 days.\n\n"
            "Want a specific topic covered first? → #suggestions",
            color=NOVA_AMBER,
        ),
    ],

    "market-structure": [
        E(
            "🧭 Market structure",
            "Discussion of structure across timeframes — daily, 4h, 1h, 15m.\n\n"
            "**Use this channel for:**\n"
            "• Daily structure analysis (BOS, MSS, CHOCH)\n"
            "• Asking whether a move was real or a stop-hunt\n"
            "• Sharing chart annotations and getting feedback\n"
            "• Tagging the founder for second-opinions\n\n"
            "**60s slowmode** to keep the noise down — write deliberately.",
        ),
    ],

    "concept-of-the-week": [
        E(
            "💡 Concept of the Week",
            "One ICT concept per week. Deep, not shallow. Examples followed by exercises.\n\n"
            "**This week — Liquidity Sweeps**\n\n"
            "**Definition:** when price quickly takes out a recent high or low to grab resting orders, "
            "then reverses.\n\n"
            "**Why it matters:** every NOVA Algo entry trigger starts with a sweep. Master sweep "
            "recognition and you'll see them everywhere.\n\n"
            "**Exercise this week:** load NQ on a 5m chart for the last 5 trading days. Mark every "
            "session high/low, weekly open, PDH, PDL. Then count how many times price *swept* one of "
            "those levels and reversed within 3 bars. Post your screenshot in #market-structure.",
            color=NOVA_PURPLE,
            footer="New concept every Monday. Last week's archives stay readable below.",
        ),
    ],

    "resource-vault": [
        E(
            "📚 Resource Vault",
            "Curated free + paid resources to supplement what you learn here.\n\n"
            "**Free:**\n"
            "• ICT Mentorship 2022 (YouTube playlists) — the source\n"
            "• @TheTradingChannel ICT explainers — clean visuals\n"
            "• `/research` on novaalgo.org — backtest writeups + edge studies\n\n"
            "**Paid:**\n"
            "• ICT Mentorship Cohort (the official one) — when it opens\n"
            "• TradingView Premium — needed for NOVA Algo's TradingView alert tier\n\n"
            "**Software:**\n"
            "• TradingView (charting + alerts)\n"
            "• TradersPost (signal → broker routing)\n"
            "• Apex Trader Funding / TopStep / Lucid Trading (prop firms)",
            footer="Add a resource? Drop it in #suggestions and we'll vet it.",
        ),
    ],

    "ask-a-coach": [
        E(
            "❓ Ask a Coach",
            "Open Q&A about ICT, NOVA Algo, structure analysis, prop firm strategy.\n\n"
            "**How to ask:**\n"
            "• One question per post — keeps threads tidy\n"
            "• Include a chart screenshot if you can\n"
            "• Tag what you've already tried\n\n"
            "**What you'll get:** founder + experienced members will weigh in. Expect responses "
            "within a few hours during US hours.\n\n"
            "**No DMs to coaches.** All Q&A happens here so everyone benefits.",
        ),
    ],

    # ── COMMUNITY ────────────────────────────────────────────────────────────
    "general": [
        E(
            "💬 General — house rules",
            "Main chat. Be cool to each other.\n\n"
            "**5s slowmode** is on to keep the rhythm.\n"
            "**No tickers, no shilling, no signal posting from outside services.**\n\n"
            "Trade talk, market reactions, banter, hellos — all welcome.",
        ),
    ],

    "wins": [
        E(
            "🏆 Wins",
            "Share trades, payouts, eval passes, account flips. **Receipts only — no claims.**\n\n"
            "**Format:**\n"
            "• Screenshot of the trade or balance\n"
            "• One line of context (which signal, which session, which account)\n"
            "• Tag the prop firm if applicable\n\n"
            "**No envy.** Everyone here is on a different stage. We celebrate each other.",
            color=NOVA_GREEN,
        ),
    ],

    "screenshots": [
        E(
            "📸 Screenshots",
            "Charts, setups, executions, anything visual.\n\n"
            "**Good fit:**\n"
            "• ICT setups you spotted in the wild\n"
            "• Your trading workspace\n"
            "• Pre-session prep\n"
            "• Post-session review charts\n\n"
            "**Not a fit:** memes (use #off-topic) or trade results (use #wins).",
        ),
    ],

    "strategy-talk": [
        E(
            "🎯 Strategy talk",
            "Trading strategy discussion that's not about a specific live trade.\n\n"
            "**Use for:**\n"
            "• Comparing ORB strategies across instruments\n"
            "• Backtest discussions\n"
            "• Risk management approaches\n"
            "• Prop firm rule arbitrage\n\n"
            "**30s slowmode** — write thoughtfully.",
        ),
    ],

    "off-topic": [
        E(
            "🎲 Off-topic",
            "Not trading. Anything else.\n\n"
            "Memes, music, sports, the gym, the food, the dog. Keep it clean — same server rules apply.",
        ),
    ],

    "coffee-chat": [
        E(
            "☕ Coffee chat",
            "Mornings, hellos, light chat, day-starters.\n\n"
            "Drop a `gm` or share what you're listening to before NY AM kicks off. "
            "This is the room that sets the tone for the trading day.",
        ),
    ],

    # ── TIER LOUNGES ─────────────────────────────────────────────────────────
    "beta-lounge": [
        E(
            "🆓 Beta lounge",
            "**You're in the first 15.**\n\n"
            "This room is for the beta cohort — free forever, full access, unlimited NOVA Algo "
            "signal routing to your TradersPost.\n\n"
            "**What we want from you:**\n"
            "• Real feedback. What works, what doesn't, what's confusing.\n"
            "• Bug reports the moment something breaks.\n"
            "• Honest takes on the strategy. Did the signal feel right? Did the routing land in time?\n\n"
            "**What you get:**\n"
            "• Signals routed forever, no charge\n"
            "• Direct line to the founder (here + #fleet-vault if you upgrade later)\n"
            "• Founding member badge that doesn't get reissued\n\n"
            "Make this thing better with us.",
            color=NOVA_CYAN,
            footer="Beta cohort · launched 2026-04-27",
        ),
    ],

    "signal-lounge": [
        E(
            "⭐ Signal tier — $97/mo",
            "Signal-tier subscribers get NOVA Algo trades routed straight to their TradersPost.\n\n"
            "This room is yours. Discuss your fills, share your prop firm context, ask routing "
            "questions, request strategy color from the founder.",
            color=NOVA_BLUE,
        ),
    ],

    "auto-lounge": [
        E(
            "🔥 Auto tier — $297/mo",
            "Auto-routed to one prop or live account. Hands-off execution. NOVA fires, your "
            "TradersPost places trades, you watch the equity move.\n\n"
            "Use this room for:\n"
            "• Multi-account setup discussion\n"
            "• Prop firm passing strategies\n"
            "• Routing edge cases (stops being touched on partial fills, etc.)\n",
            color=Color(0xF97316),
        ),
    ],

    "fleet-lounge": [
        E(
            "💎 Fleet tier — $997/mo",
            "Up to 5 accounts fanned out. The serious tier.\n\n"
            "**What's in this room:**\n"
            "• Direct founder access (in here AND #fleet-vault)\n"
            "• Pre-release strategy updates before public changelog\n"
            "• Quarterly performance reviews with the founder\n"
            "• First look at new instruments + new sessions\n\n"
            "Welcome.",
            color=NOVA_CYAN,
        ),
    ],

    "fleet-vault": [
        E(
            "🔒 Fleet Vault — direct line to the founder",
            "**Fleet-tier exclusive.** No filters, no gatekeepers.\n\n"
            "Drop in here when you want:\n"
            "• A 1:1 take on your trading\n"
            "• A prop firm strategy review\n"
            "• An honest assessment of whether NOVA fits your style\n"
            "• Anything that doesn't fit a public channel\n\n"
            "Founder responds within 24h. Less, usually.",
            color=NOVA_CYAN,
        ),
    ],

    # ── TOOLS & UTILITIES ────────────────────────────────────────────────────
    "bot-commands": [
        E(
            "🤖 Bot commands",
            "Test slash-commands here. This channel is for bot interaction, not chat.\n\n"
            "**Available commands** *(rolling out)*:\n"
            "• `/levels` — pulls today's PDH, PDL, weekly open, range\n"
            "• `/sizer <stop_dollars>` — risk-to-contract calculator\n"
            "• `/status` — Railway uptime + last-signal timestamp\n"
            "• `/equity` — current fleet equity snapshot\n\n"
            "More commands shipping weekly.",
        ),
    ],

    "position-sizer": [
        E(
            "🧮 Position sizer — NQ futures",
            "Quick reference for risk per contract on NQ.\n\n"
            "**NQ contract specs:**\n"
            "• Tick size: 0.25\n"
            "• Tick value: $5\n"
            "• Point value: $20\n\n"
            "**NOVA standard risk: $500 stop = 25 NQ points.**\n"
            "(25 points × $20/point = $500/contract)\n\n"
            "**Quick math by SL distance:**\n"
            "• 10 pts SL = $200/contract\n"
            "• 15 pts SL = $300/contract\n"
            "• 20 pts SL = $400/contract\n"
            "• 25 pts SL = $500/contract ← **NOVA standard**\n"
            "• 30 pts SL = $600/contract\n"
            "• 50 pts SL = $1,000/contract\n\n"
            "**Sizing rule (1% account risk):**\n"
            "• $50K account → $500 max risk → 1 contract at NOVA standard\n"
            "• $100K account → $1000 max risk → 2 contracts at NOVA standard\n"
            "• $250K account → $2500 max risk → 5 contracts at NOVA standard",
            footer="Always size for the worst-case stop, never the best-case.",
        ),
    ],

    "economic-calendar": [
        E(
            "📅 Economic calendar",
            "Auto-posted high-impact macro events.\n\n"
            "**What you'll see here this week:**\n"
            "• FOMC events (rate decisions, minutes)\n"
            "• CPI / PCE / PPI prints\n"
            "• NFP (first Friday of month)\n"
            "• Unemployment rate\n"
            "• Powell speeches\n\n"
            "**The rule:** if there's an 8:30am ET print on a session day, NOVA's gates "
            "tighten — sometimes the algo skips the session entirely. Watch this channel.",
        ),
    ],

    "stats-dashboard": [
        E(
            "📊 Live stats dashboard",
            "Live performance numbers, refreshed daily.",
            color=NOVA_GREEN,
        ),
        E(
            "Cumulative — since 2025-11-02",
            None,
            color=NOVA_GREEN,
            fields=[
                {"name": "Trades",     "value": "**241**", "inline": True},
                {"name": "Win rate",   "value": "**53.5%**", "inline": True},
                {"name": "Profit factor", "value": "**1.4**", "inline": True},
                {"name": "Expectancy", "value": "**+0.19R per trade**", "inline": True},
                {"name": "Max DD",     "value": "**-7.2R**", "inline": True},
                {"name": "Best day",   "value": "**+5.2R**", "inline": True},
            ],
            footer="Live forward-test since 2026-04-20 · backtest covers 2025-11-02 → 2026-04-21.",
        ),
    ],

    "backtest-results": [
        E(
            "🧪 Backtest results",
            "Historical NOVA Algo performance across the full backtest window.\n\n"
            "**Methodology:**\n"
            "• NQ futures, 15m bars, NY AM session only\n"
            "• Full ICT confluence stack required (sweep + MSS + bias alignment)\n"
            "• $500 SL / $1500 TP / BE at 1R / trail 1R after BE\n"
            "• 2 contracts per signal, $20/point value\n"
            "• Realistic slippage (1 tick) and commission ($4 RT) modeled\n\n"
            "**Window:** 2025-11-02 → 2026-04-21 (5.5 months, 241 trades).\n\n"
            "Full equity curve + month-by-month breakdown: https://novaalgo.org/performance",
            color=NOVA_GREEN,
            footer="No backtest survives contact with reality unchanged. Forward-test live since 2026-04-20.",
        ),
    ],

    # ── SUPPORT ──────────────────────────────────────────────────────────────
    "open-ticket": [
        E(
            "🎫 Open a support ticket",
            "Need help that's *not* a public question?\n\n"
            "**Drop a message in this channel describing:**\n"
            "• What you're trying to do\n"
            "• What's going wrong\n"
            "• Screenshots if relevant\n\n"
            "A staff member will spawn a private thread off your message and we'll handle it 1:1.\n\n"
            "**Ticket-worthy issues:**\n"
            "• TradersPost webhook isn't firing\n"
            "• Missing signal you should have received\n"
            "• Billing/payment issues\n"
            "• Account access / Clerk login problems\n\n"
            "**Not ticket-worthy** (post in public channels instead):\n"
            "• \"What does this signal mean?\" → #ask-a-coach\n"
            "• \"Is this setup ICT-valid?\" → #market-structure\n"
            "• \"How do I configure my prop firm?\" → #onboarding-tradespost",
            color=NOVA_AMBER,
        ),
    ],

    "bug-reports": [
        E(
            "🐛 Bug reports",
            "Found a bug? Help us squash it.\n\n"
            "**Useful report:**\n"
            "• What you did (steps to reproduce)\n"
            "• What you expected\n"
            "• What actually happened\n"
            "• Browser/device if it's site-related\n"
            "• Screenshot or log paste\n\n"
            "**Site bugs:** novaalgo.org rendering, /portal issues, sign-up flow.\n"
            "**Discord bugs:** wrong perms, missing channels, wrong roles.\n"
            "**Signal bugs:** missed alert, wrong SL/TP, route to wrong account.\n\n"
            "Founder reads this channel personally.",
            color=NOVA_RED,
        ),
    ],

    "suggestions": [
        E(
            "💡 Suggestions",
            "What should NOVA Algo do next?\n\n"
            "• Strategy ideas (new sessions, new instruments — within scope)\n"
            "• Tooling additions (mobile push, calendar integrations)\n"
            "• Education topics for #concept-of-the-week\n"
            "• Site features for novaalgo.org\n"
            "• Discord features\n\n"
            "**Best feedback comes from active subscribers.** What would make this better for you?",
        ),
    ],

    "feedback": [
        E(
            "📮 Feedback",
            "Anonymous-friendly feedback drop.\n\n"
            "Tell us:\n"
            "• What's working really well that we should *do more of*\n"
            "• What feels off, awkward, or confusing\n"
            "• Where you almost-bounced and what saved you\n"
            "• What you'd tell a friend about NOVA Algo\n\n"
            "Honest > polite. We grow from the rough edges.",
        ),
    ],

    "contact-founder": [
        E(
            "📞 Contact the founder",
            "Direct line to the founder. **Use sparingly — high-priority items only.**\n\n"
            "**Good fit:**\n"
            "• Partnership/collab opportunities\n"
            "• Press / interview requests\n"
            "• Major billing escalations\n"
            "• Time-sensitive signal issues if support is slow\n\n"
            "**Not a fit:** general questions (use #ask-a-coach), bugs (use #bug-reports), "
            "feature requests (use #suggestions).\n\n"
            "Founder reads here daily. Response within 24h, usually faster.",
            footer="Email backup: founder@novaalgo.org",
        ),
    ],

    # ── STAFF (founder-only) ─────────────────────────────────────────────────
    "founder-notes": [
        E(
            "📓 Founder notes",
            "Sir's private scratch. Drafts, ideas, observations, rants.\n\n"
            "Not visible to members. Not visible to staff. Just you and the algo.",
        ),
    ],

    "bot-logs": [
        E(
            "🔧 Bot logs",
            "Audit log for all bot actions on this server.\n\n"
            "Tracks:\n"
            "• Channel/role create/delete events\n"
            "• Permission overwrite changes\n"
            "• Webhook posts and failures\n"
            "• Rate-limit warnings\n\n"
            "Auto-posted by the NOVA Bridge bot.",
        ),
    ],

    "signal-audit": [
        E(
            "🔎 Signal audit log",
            "Every webhook event Railway processes — accepted, rejected, errored.\n\n"
            "Use this when you need to answer:\n"
            "• Did the signal even arrive?\n"
            "• Why was it rejected?\n"
            "• Which gate tripped?\n"
            "• What did the payload look like?\n\n"
            "Founder-only. No member visibility on raw payloads.",
        ),
    ],

    "halt-events": [
        E(
            "🛑 Halt events",
            "Auto-posts every time NOVA's halt gate activates.\n\n"
            "**Triggers:**\n"
            "• Daily loss limit hit ($500 default)\n"
            "• Daily trade limit hit (5 default)\n"
            "• Manual halt activation via /admin/halt\n"
            "• Dispatch failure across all venues\n\n"
            "When this channel pings — investigate before next session.",
            color=NOVA_RED,
        ),
    ],

    "fanout-failures": [
        E(
            "⚠ Fanout failures",
            "Subscriber webhook delivery failures.\n\n"
            "Auto-posts when a fanout result has any non-2xx subscriber response. Includes:\n"
            "• Which subscriber failed\n"
            "• HTTP status code\n"
            "• Response body (truncated)\n\n"
            "Investigate before next signal — usually a bad webhook URL or TradersPost outage.",
            color=NOVA_AMBER,
        ),
    ],

    "partnership-inbox": [
        E(
            "🤝 Partnership inbox",
            "Inbound partnership inquiries forwarded from founder@novaalgo.org and contact-founder.\n\n"
            "Triage rules:\n"
            "• Prop firm partnerships → priority\n"
            "• Affiliate proposals → batch weekly\n"
            "• Press/interview → 48h response\n"
            "• Reseller/white-label → defer until tier-3 launch",
        ),
    ],
}


async def has_messages(channel: discord.TextChannel) -> bool:
    try:
        async for _ in channel.history(limit=1):
            return True
    except discord.Forbidden:
        return True  # if we can't read, assume populated (don't double-post)
    return False


@client.event
async def on_ready():
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print("guild not found"); await client.close(); return

    print(f"Connected to {guild.name}\n")

    posted_count = 0
    skipped_count = 0
    failed_count = 0

    for channel_name, embeds in CONTENT.items():
        ch = discord.utils.get(guild.text_channels, name=channel_name)
        if not ch:
            print(f"  · #{channel_name} — channel not found, skipping")
            continue

        if await has_messages(ch):
            print(f"  · #{channel_name} (has content, skipping {len(embeds)} embed{'s' if len(embeds) != 1 else ''})")
            skipped_count += 1
            continue

        print(f"  ✓ #{channel_name} ({len(embeds)} embed{'s' if len(embeds) != 1 else ''})")
        for embed in embeds:
            try:
                await ch.send(embed=embed)
                posted_count += 1
                await asyncio.sleep(0.6)  # rate-limit gentleness
            except discord.HTTPException as e:
                print(f"      ✗ embed failed: {e}")
                failed_count += 1

    print(f"\n✓ posted {posted_count} embeds across {len(CONTENT) - skipped_count} channels")
    print(f"  skipped {skipped_count} channels (already had content)")
    if failed_count:
        print(f"  ⚠ {failed_count} embeds failed")

    await client.close()


def main():
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
