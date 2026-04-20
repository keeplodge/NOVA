"""
Trade Memorializer — bridges Railway's /agents/ledger to the Neural Brain.

Railway is remote and can't reach Sir's local Neural Brain at :7337.
This daemon runs on the desktop (inside NOVA Assistant's process), polls
Railway's ledger every 30s, and for each new signal_executed or
signal_escalated event, writes a rich memory into the Brain.

Memory shape:
    category: "trading"
    summary:  "NQ1! BUY @ 26,840 · London · A+ · executed"
    content:  full trade details incl. sl/tp/be/grade/sweep/dispatch chain
    tags:     ["trade", ticker, action, session, grade, outcome]

Persists last-seen signal_id across restarts in ~workspace/trade_memorializer_state.json
so a restart doesn't re-memorialize the same trade.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("trade-memorializer")

RAILWAY_URL  = os.environ.get("NOVA_SERVER_URL", "https://nova-production-72f5.up.railway.app")
BRAIN_URL    = os.environ.get("NOVA_BRAIN_URL",  "http://127.0.0.1:7337")
POLL_INTERVAL = int(os.environ.get("NOVA_TRADE_POLL_S", "30"))
STATE_FILE   = Path(__file__).parent / ".trade_memorializer_state.json"


class TradeMemorializer:

    def __init__(self):
        self._stop   = threading.Event()
        self._thread = None
        self._seen   = self._load_state()

    # ── State persistence ──────────────────────────────────────────────
    def _load_state(self) -> set[str]:
        if not STATE_FILE.exists():
            return set()
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return set(data.get("seen_signal_ids", []))
        except Exception:
            return set()

    def _save_state(self):
        try:
            STATE_FILE.write_text(
                json.dumps({"seen_signal_ids": sorted(self._seen)[-500:]}, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"state save failed: {e}")

    # ── Railway ledger fetch ──────────────────────────────────────────
    def _fetch_ledger(self, limit: int = 50) -> List[dict]:
        try:
            r = httpx.get(f"{RAILWAY_URL}/agents/ledger",
                          params={"limit": limit}, timeout=5.0)
            r.raise_for_status()
            return r.json().get("entries", [])
        except Exception as e:
            logger.warning(f"ledger fetch failed: {e}")
            return []

    # ── Memory POST ───────────────────────────────────────────────────
    def _post_memory(self, mem: Dict[str, Any]) -> Optional[str]:
        try:
            r = httpx.post(f"{BRAIN_URL}/memory", json=mem, timeout=5.0)
            r.raise_for_status()
            data = r.json()
            return data.get("id")
        except Exception as e:
            logger.warning(f"brain memory post failed: {e}")
            return None

    # ── Formatting ────────────────────────────────────────────────────
    def _build_memory(self, entry: dict) -> Dict[str, Any]:
        """
        Convert a ledger `signal_executed` or `signal_escalated` entry into
        a Neural Brain memory.
        """
        event     = entry.get("event", "")
        sid       = entry.get("signal_id", "?")
        ticker    = entry.get("ticker", "?")
        ts        = entry.get("ts", "")
        chosen    = entry.get("chosen", "")

        # Try to pull richer details if present (depends on observability payload)
        extras = {k: v for k, v in entry.items()
                  if k not in ("event", "ts", "signal_id", "ticker", "chosen")}

        status_label = "executed" if event == "signal_executed" else "escalated"
        summary = f"{ticker} · {status_label} · signal {sid}"
        if chosen:
            summary += f" via {chosen}"

        try:
            ts_readable = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            ts_readable = ts

        content_lines = [
            f"**Trade {status_label}** — {ts_readable}",
            f"Signal ID: `{sid}`",
            f"Ticker: **{ticker}**",
        ]
        if chosen:
            content_lines.append(f"Dispatch venue: `{chosen}`")
        if extras:
            content_lines.append("")
            content_lines.append("**Details:**")
            for k, v in extras.items():
                content_lines.append(f"- {k}: `{v}`")

        return {
            "category": "trading",
            "summary":  summary,
            "content":  "\n".join(content_lines),
            "tags":     ["trade", ticker, status_label, "nova-auto"],
        }

    # ── Tick ──────────────────────────────────────────────────────────
    def tick(self) -> int:
        """
        One polling cycle. Returns number of new trades memorialized.
        """
        entries = self._fetch_ledger(limit=50)
        new_count = 0
        # Walk oldest-first so memories land in chronological order
        for entry in reversed(entries):
            event = entry.get("event")
            if event not in ("signal_executed", "signal_escalated"):
                continue
            sid = entry.get("signal_id")
            if not sid or sid in self._seen:
                continue
            mem_payload = self._build_memory(entry)
            mem_id = self._post_memory(mem_payload)
            if mem_id:
                self._seen.add(sid)
                new_count += 1
                logger.info(f"memorialized trade signal={sid} -> brain memory={mem_id}")
            else:
                logger.warning(f"failed to memorialize signal={sid}")
        if new_count:
            self._save_state()
        return new_count

    # ── Daemon loop ──────────────────────────────────────────────────
    def _loop(self):
        # Initial delay lets Brain finish booting
        time.sleep(5)
        while not self._stop.is_set():
            try:
                n = self.tick()
                if n:
                    logger.info(f"tick: {n} new trade(s) memorialized")
            except Exception as e:
                logger.exception(f"tick error: {e}")
            self._stop.wait(POLL_INTERVAL)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="TradeMemorializer", daemon=True)
        self._thread.start()
        logger.info(f"TradeMemorializer started — polling Railway every {POLL_INTERVAL}s, "
                    f"bridging to Brain at {BRAIN_URL}")

    def stop(self):
        self._stop.set()


# Module-level singleton
_agent: TradeMemorializer | None = None

def get_agent() -> TradeMemorializer:
    global _agent
    if _agent is None:
        _agent = TradeMemorializer()
    return _agent
