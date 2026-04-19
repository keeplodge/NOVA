"""
NOVA Drift Monitor — proactive performance degradation detection.

Runs as a background thread inside nova_assistant.py (spawned from main()),
polling nova_brain.db every DRIFT_CHECK_INTERVAL seconds. When it detects
strategy drift — a rolling win-rate drop, a consecutive-loss streak, or a
sharp PnL drawdown — it fires a voice alert via the shared speak() function
and writes a `trading:drift:<date>` memory to the Neural Brain.

Design notes:
- Non-blocking: runs on its own daemon thread, never touches the scheduler.
- Idempotent: tracks the last alert signature per-day so Sir doesn't get
  the same warning every 60 min.
- Graceful: degrades to silent no-op if the DB is missing, the Brain is
  offline, or speak is not importable.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Callable
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Wire in brain_bridge
_BRAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neural-brain", "backend")
if _BRAIN_PATH not in sys.path:
    sys.path.insert(0, _BRAIN_PATH)

try:
    from brain_bridge import remember as _brain_remember
    _BRAIN_ENABLED = True
except Exception:
    _BRAIN_ENABLED = False
    def _brain_remember(*_a, **_kw): return None


logger = logging.getLogger(__name__)

DB_PATH                = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nova_brain.db")
DRIFT_CHECK_INTERVAL   = 3600        # 60 min
WINDOW_SIZE            = 20          # rolling window for win-rate comparison
WINRATE_DROP_THRESHOLD = 0.05        # 5 percentage point drop triggers alert
LOSS_STREAK_THRESHOLD  = 3           # 3 consecutive losses triggers alert
DRAWDOWN_R_THRESHOLD   = -6.0        # cumulative R-multiple drop over last 10 trades
EST = ZoneInfo("America/New_York")


@dataclass
class DriftAlert:
    kind:      str       # 'winrate_drop' | 'loss_streak' | 'drawdown'
    severity:  str       # 'warning' | 'critical'
    headline:  str       # one-line for voice
    detail:    str       # longer for Brain memory
    metric:    float
    signature: str       # dedup key, day-scoped


class DriftMonitor:
    """Single instance per running assistant; call .run() on a daemon thread."""

    def __init__(
        self,
        db_path:    str = DB_PATH,
        speaker:    Callable[[str], None] | None = None,
        interval:   int = DRIFT_CHECK_INTERVAL,
    ):
        self.db_path           = db_path
        self.speaker           = speaker       # usually nova_assistant.speak
        self.interval          = interval
        self._stop_flag        = threading.Event()
        self._alerted_today    = set()          # signatures we've already alerted on today
        self._current_day      = None
        self._last_check_ts    = 0.0

    # ────────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────────

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="drift-monitor")
        t.start()
        logger.info(f"Drift monitor started — checking every {self.interval}s")
        return t

    def stop(self) -> None:
        self._stop_flag.set()

    def run(self) -> None:
        """Main loop. Runs forever on a daemon thread."""
        while not self._stop_flag.is_set():
            try:
                alerts = self.check()
                for a in alerts:
                    self._handle_alert(a)
            except Exception as e:
                logger.error(f"drift check failed: {e}")
            # Sleep in small increments so stop() is responsive
            for _ in range(self.interval):
                if self._stop_flag.is_set():
                    return
                time.sleep(1)

    def check(self) -> list[DriftAlert]:
        """Run all drift checks once. Returns a list of active alerts."""
        self._reset_day_scope()
        trades = self._load_closed_trades(limit=200)
        if len(trades) < 5:
            return []

        alerts: list[DriftAlert] = []
        alerts.extend(self._check_winrate_drop(trades))
        alerts.extend(self._check_loss_streak(trades))
        alerts.extend(self._check_drawdown(trades))
        self._last_check_ts = time.time()
        return alerts

    # ────────────────────────────────────────────────────────────────────
    # Individual checks
    # ────────────────────────────────────────────────────────────────────

    def _check_winrate_drop(self, trades: list[dict]) -> list[DriftAlert]:
        """Compares last WINDOW_SIZE trades vs. the WINDOW_SIZE before that."""
        if len(trades) < WINDOW_SIZE * 2:
            return []
        recent = trades[:WINDOW_SIZE]
        prior  = trades[WINDOW_SIZE:WINDOW_SIZE * 2]
        recent_wr = _win_rate(recent)
        prior_wr  = _win_rate(prior)
        drop      = prior_wr - recent_wr
        if drop < WINRATE_DROP_THRESHOLD:
            return []
        severity = "critical" if drop >= 0.15 else "warning"
        return [DriftAlert(
            kind="winrate_drop",
            severity=severity,
            headline=(
                f"Win rate dropped {drop*100:.0f} points, Sir. "
                f"Last {WINDOW_SIZE} trades at {recent_wr*100:.0f} percent "
                f"versus prior {WINDOW_SIZE} at {prior_wr*100:.0f}."
            ),
            detail=(
                f"Strategy drift detected. Rolling win-rate comparison:\n"
                f"- Last {WINDOW_SIZE} trades: {recent_wr*100:.1f}% ({_count_outcomes(recent)})\n"
                f"- Prior {WINDOW_SIZE} trades: {prior_wr*100:.1f}% ({_count_outcomes(prior)})\n"
                f"- Drop: {drop*100:.1f} percentage points.\n"
                f"Recommend a pattern-review pass and possible strategy pause until cause is identified."
            ),
            metric=drop,
            signature=f"winrate_drop_{severity}",
        )]

    def _check_loss_streak(self, trades: list[dict]) -> list[DriftAlert]:
        """Detects N consecutive losses at the front of the trade list."""
        streak = 0
        for t in trades:
            outcome = (t.get("outcome") or "").lower()
            if outcome == "loss":
                streak += 1
            else:
                break
        if streak < LOSS_STREAK_THRESHOLD:
            return []
        severity = "critical" if streak >= 5 else "warning"
        return [DriftAlert(
            kind="loss_streak",
            severity=severity,
            headline=(
                f"Sir, you are on {streak} consecutive losses. "
                f"Step away from the chart. Let the next setup come to you."
            ),
            detail=(
                f"Consecutive loss streak of {streak} trades. "
                f"Historical drawdown psychology risk zone. "
                f"Recommend: no further entries until a session break, "
                f"review pattern for common failure mode, "
                f"check mindset / fatigue / revenge indicators."
            ),
            metric=float(streak),
            signature=f"loss_streak_{streak}",
        )]

    def _check_drawdown(self, trades: list[dict]) -> list[DriftAlert]:
        """Cumulative R-multiple drop over the last 10 trades."""
        if len(trades) < 10:
            return []
        window = trades[:10]
        r_sum = sum((t.get("r_multiple") or 0.0) for t in window)
        if r_sum > DRAWDOWN_R_THRESHOLD:
            return []
        severity = "critical" if r_sum <= -8.0 else "warning"
        return [DriftAlert(
            kind="drawdown",
            severity=severity,
            headline=(
                f"Drawdown alert, Sir. Last 10 trades total {r_sum:.1f} R. "
                f"Significantly below neutral."
            ),
            detail=(
                f"Cumulative R-multiple over last 10 trades: {r_sum:.2f} R.\n"
                f"Threshold for this alert: {DRAWDOWN_R_THRESHOLD} R.\n"
                f"Outcomes breakdown: {_count_outcomes(window)}.\n"
                f"Recommendation: consider a 24-48h break, review session selection, "
                f"re-check grade filter (currently A+ / A only)."
            ),
            metric=r_sum,
            signature=f"drawdown_{severity}",
        )]

    # ────────────────────────────────────────────────────────────────────
    # Alert handling
    # ────────────────────────────────────────────────────────────────────

    def _handle_alert(self, a: DriftAlert) -> None:
        if a.signature in self._alerted_today:
            logger.debug(f"drift alert {a.signature} already fired today — skipping")
            return
        self._alerted_today.add(a.signature)

        logger.warning(f"DRIFT [{a.severity}] {a.kind}: {a.headline}")

        # Voice alert
        if self.speaker:
            try:
                self.speaker(a.headline)
            except Exception as e:
                logger.error(f"drift speaker failed: {e}")

        # Brain memory
        if _BRAIN_ENABLED:
            try:
                date_str = datetime.now(tz=EST).strftime("%Y-%m-%d")
                _brain_remember(
                    content=a.detail,
                    category="trading",
                    summary=f"Drift [{a.severity}] — {a.kind.replace('_', ' ')} on {date_str}",
                    tags=[f"trading:drift:{date_str}", "drift", a.kind, a.severity],
                )
            except Exception as e:
                logger.error(f"drift brain write failed: {e}")

    def _reset_day_scope(self) -> None:
        today = datetime.now(tz=EST).date()
        if today != self._current_day:
            self._current_day = today
            self._alerted_today.clear()

    # ────────────────────────────────────────────────────────────────────
    # Data layer
    # ────────────────────────────────────────────────────────────────────

    def _load_closed_trades(self, limit: int = 200) -> list[dict]:
        """Most recent closed trades (win/loss/be), newest first."""
        if not os.path.exists(self.db_path):
            return []
        try:
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            rows = c.execute("""
                SELECT id, created_at, date, session, direction, outcome, pnl, r_multiple, grade
                FROM trades
                WHERE outcome IN ('win', 'loss', 'be')
                ORDER BY id DESC
                LIMIT ?
            """, (limit,)).fetchall()
            c.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"drift db read failed: {e}")
            return []


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t.get("outcome") or "").lower() == "win")
    return wins / len(trades)


def _count_outcomes(trades: list[dict]) -> str:
    w = sum(1 for t in trades if (t.get("outcome") or "").lower() == "win")
    l = sum(1 for t in trades if (t.get("outcome") or "").lower() == "loss")
    be = sum(1 for t in trades if (t.get("outcome") or "").lower() == "be")
    return f"{w}W / {l}L / {be}BE"


# ═══════════════════════════════════════════════════════════════════════════
# CLI — quick one-shot check for debugging
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    m = DriftMonitor(speaker=lambda s: print(f"[SPEAK] {s}"))
    alerts = m.check()
    print(f"Closed trades in DB: {len(m._load_closed_trades(200))}")
    print(f"Alerts: {len(alerts)}")
    for a in alerts:
        print(f"  [{a.severity}] {a.kind}: {a.headline}")
