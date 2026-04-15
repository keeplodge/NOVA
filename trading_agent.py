#!/usr/bin/env python3
"""
trading_agent.py — NOVA trade monitor and session watcher

Responsibilities:
  - Poll Railway /status every 30 seconds
  - Detect new trades and announce via voice + flash GUI green
  - Log detected trades to Obsidian 01_Trade_Logs
  - Detect session open/close transitions and announce
  - Monitor daily loss and trigger threshold alerts at $300 / $450 / $500

Designed to run as a background thread inside nova_local.py.
Can also run standalone for testing.
"""

import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ── Config ─────────────────────────────────────────────────────────────────────
NOVA_SERVER_URL    = os.environ.get(
    "NOVA_SERVER_URL", "https://nova-production-72f5.up.railway.app"
)
OBSIDIAN_TRADE_LOG_DIR = os.environ.get(
    "OBSIDIAN_TRADE_LOG_DIR",
    r"C:\Users\User\nova\nova-brain\01_Trade_Logs",
)
POLL_INTERVAL = 30   # seconds
EST           = ZoneInfo("America/New_York")

# ── Colours (mirrored from nova_local.py for GUI push calls) ──────────────────
C_BG      = "#020408"
C_CYAN    = "#00d4ff"
C_GREEN   = "#00ff88"
C_RED     = "#ff3355"
C_WHITE   = "#FFFFFF"

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("trading_agent")

# ── Session helpers ───────────────────────────────────────────────────────────
SESSION_DISPLAY = {
    "Asia":   "Asia",
    "London": "London",
    "NY_AM":  "NY",
}

LOSS_THRESHOLDS = [
    (300, "Sir, daily loss is at three hundred dollars. Proceed with caution."),
    (450, "Sir, approaching the daily loss limit. One trade remaining in the budget."),
    (500, "Sir, daily loss limit reached. NOVA is standing down."),
]


# ── Obsidian trade log ────────────────────────────────────────────────────────

RISK_PER_TRADE   = 500.0
REWARD_PER_TRADE = 1000.0

SIDE_MAP = {"buy": "long", "sell": "short"}
TYPE_MAP = {"first": "First entry", "cont": "Continuation", "continuation": "Continuation"}


def _write_trade_log(session: str, last_signal: dict | None, now: datetime) -> str | None:
    """
    Create a markdown trade log in the Obsidian vault.
    Uses last_signal from /status for side and price; falls back to TBD if absent.
    Returns the file path on success, None on failure.
    """
    os.makedirs(OBSIDIAN_TRADE_LOG_DIR, exist_ok=True)

    side  = "TBD"
    price = "TBD"

    if last_signal:
        side  = SIDE_MAP.get(last_signal.get("action", ""), "TBD")
        price = last_signal.get("price", "TBD")

    session_label = SESSION_DISPLAY.get(session, session)
    filename      = now.strftime("%Y-%m-%d-%H-%M") + f"-{session.lower().replace('_', '')}-{side}.md"
    path          = os.path.join(OBSIDIAN_TRADE_LOG_DIR, filename)

    content = f"""# Trade Log — {now.strftime("%Y-%m-%d %H:%M")} EST

**Date:** {now.strftime("%Y-%m-%d")}
**Time:** {now.strftime("%H:%M")} EST
**Session:** {session_label}
**Side:** {side.capitalize() if side != "TBD" else "TBD"}
**Type:** TBD

---

## Entry

| Field        | Value         |
|--------------|---------------|
| Entry Price  | {price}       |
| Stop Loss    | TBD           |
| Take Profit  | TBD           |
| Risk         | ${RISK_PER_TRADE:.0f}       |
| Reward       | ${REWARD_PER_TRADE:.0f}      |
| R:R          | 1:2           |

---

## Result

| Field       | Value  |
|-------------|--------|
| Status      | Open   |
| Exit Price  | —      |
| Outcome     | —      |
| P&L         | —      |

---

## Notes

_Add post-trade notes here._
"""

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[trading_agent] Obsidian log created: {path}")
        return path
    except Exception as e:
        logger.error(f"[trading_agent] Failed to write Obsidian log: {e}")
        return None


# ── TradingAgent ──────────────────────────────────────────────────────────────

class TradingAgent:
    """
    Polls the NOVA Railway server and reacts to trade events, session changes,
    and daily loss thresholds.

    Inject speak_fn and push_gui_fn at construction time so the agent is
    decoupled from the GUI — it can run headless (unit tests, standalone mode)
    or wired into nova_local.py's GUI-aware versions.

    speak_fn(text: str)                         — speak a line of text
    push_gui_fn(mode: str, color: str, status)  — update the waveform UI
    """

    def __init__(
        self,
        speak_fn,
        push_gui_fn=None,
        server_url: str = NOVA_SERVER_URL,
        obsidian_dir: str = OBSIDIAN_TRADE_LOG_DIR,
    ):
        self._speak    = speak_fn
        self._push_gui = push_gui_fn or (lambda mode, color, status="": None)
        self._url      = server_url.rstrip("/")
        self._obs_dir  = obsidian_dir

        # Polling state — populated on first successful poll
        self._first_poll         = True
        self._prev_session_trades: dict[str, int] = {}
        self._prev_active_session: str | None      = None
        self._loss_alerts_sent:    set[int]        = set()

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self) -> threading.Thread:
        """Spawn and return the background daemon thread."""
        t = threading.Thread(target=self._run, daemon=True, name="trading-agent")
        t.start()
        logger.info("[trading_agent] background thread started")
        return t

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _run(self):
        while True:
            try:
                self._poll()
            except Exception as e:
                logger.error(f"[trading_agent] poll error: {e}")
            time.sleep(POLL_INTERVAL)

    def _poll(self):
        try:
            r = requests.get(f"{self._url}/status", timeout=8)
        except requests.exceptions.RequestException as e:
            logger.debug(f"[trading_agent] server unreachable: {e}")
            return

        if not r.ok:
            logger.debug(f"[trading_agent] bad status response: {r.status_code}")
            return

        data = r.json()

        session_trades  = data.get("session_trades", {})
        active_session  = data.get("active_session", "None")
        daily_loss      = float(data.get("daily_loss", 0.0))
        last_signal     = data.get("last_signal")   # None or {"action", "price", "session", "ticker"}

        # ── First poll — seed state without triggering events ─────────────────
        if self._first_poll:
            self._prev_session_trades = dict(session_trades)
            self._prev_active_session = active_session
            self._first_poll          = False
            logger.info(
                f"[trading_agent] seeded — session: {active_session} | "
                f"trades: {session_trades} | loss: ${daily_loss:.0f}"
            )
            return

        now = datetime.now(tz=EST)

        # ── 1. Detect new trades ──────────────────────────────────────────────
        for session, count in session_trades.items():
            prev = self._prev_session_trades.get(session, 0)
            if count > prev:
                self._on_new_trade(session, last_signal, now)

        self._prev_session_trades = dict(session_trades)

        # ── 2. Detect session changes ─────────────────────────────────────────
        if active_session != self._prev_active_session:
            self._on_session_change(active_session)
            self._prev_active_session = active_session

        # ── 3. Loss threshold alerts ──────────────────────────────────────────
        self._check_loss_alerts(daily_loss)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_new_trade(self, session: str, last_signal: dict | None, now: datetime):
        session_label = SESSION_DISPLAY.get(session, session)

        if last_signal and last_signal.get("action"):
            side  = SIDE_MAP.get(last_signal["action"], "position")
            price = last_signal.get("price")
            msg   = f"Sir, NOVA has entered a {side} position in the {session_label} session"
            if price:
                msg += f" at {price}"
            msg += "."
        else:
            msg = f"Sir, NOVA has entered a position in the {session_label} session."

        logger.info(f"[trading_agent] new trade detected — {session_label} | {msg}")

        # Flash GUI green
        self._push_gui("trade", C_GREEN, f"Trade executed — {session_label} session")

        # Announce
        self._speak(msg)

        # Log to Obsidian
        log_path = _write_trade_log(session, last_signal, now)
        if log_path:
            logger.info(f"[trading_agent] Obsidian log: {os.path.basename(log_path)}")

        # Return waveform to listening after 5 seconds
        threading.Timer(
            5.0, lambda: self._push_gui("listening", C_CYAN, "Listening...")
        ).start()

    def _on_session_change(self, new_session: str):
        announcements = {
            "Asia":   "Sir, Asia session is now open.",
            "London": "Sir, London session is now open.",
            "NY_AM":  "Sir, NY session is now open.",
            "None":   "Sir, the trading session is now closed.",
        }
        msg = announcements.get(new_session, f"Sir, session changed to {new_session}.")
        logger.info(f"[trading_agent] session change → {new_session}")
        self._speak(msg)

    def _check_loss_alerts(self, daily_loss: float):
        for threshold, message in LOSS_THRESHOLDS:
            if daily_loss >= threshold and threshold not in self._loss_alerts_sent:
                self._loss_alerts_sent.add(threshold)
                logger.warning(f"[trading_agent] loss threshold hit: ${threshold}")
                self._push_gui("alert", C_RED, f"Daily loss: ${daily_loss:.0f}")
                self._speak(message)
                break   # one alert per poll cycle — next threshold fires on next breach


# ── Standalone entry point (for testing without the GUI) ─────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    def _print_speak(text: str):
        print(f"\n[NOVA]: {text}\n")

    agent = TradingAgent(speak_fn=_print_speak)
    agent.start()

    print(f"Trading agent running — polling {NOVA_SERVER_URL} every {POLL_INTERVAL}s")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
