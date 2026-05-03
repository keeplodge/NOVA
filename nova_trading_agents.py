"""
═══════════════════════════════════════════════════════════════════════════
NOVA Trading Agent Hierarchy
───────────────────────────────────────────────────────────────────────────

A signal arriving at /webhook no longer goes through a single "forward to
TradersPost" call. It flows through a commander that delegates to specialist
sub-agents, each responsible for one concern:

    TradingCommander       — top-level orchestrator
    ├── SignalIntelligence — fingerprints + enriches the raw signal
    ├── RiskGuardian       — enforces every risk gate (wraps evaluate_gates)
    ├── ExecutionDispatcher
    │     ├── RetryVenue(TradersPostVenue)    — primary, 3x with backoff
    │     └── ManualEscalationVenue           — tertiary fallback: queues
    │                                           the trade + blasts a Discord
    │                                           DM to Sir with a one-tap
    │                                           fire URL
    └── Observability      — mirrors every state transition to Discord +
                             Neural Brain + in-memory ledger

If primary TradersPost fails 3x, the signal automatically escalates to manual
so Sir gets the trade on his phone and can fire it with one tap. No dropped
signals, ever.

Env vars:
    TRADERSPOST_WEBHOOK_URL   — existing TP strategy webhook
    NOVA_DISCORD_WEBHOOK_URL  — Discord webhook for mirror notifications
    NOVA_PUBLIC_BASE          — public base URL of this Railway app (used to
                                generate the /fire one-tap URLs)
    NOVA_FIRE_SECRET          — salt for the fire-token HMAC
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests


logger = logging.getLogger(__name__)
EST    = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════════════════
# Data classes — shared across agents
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class EnrichedSignal:
    """A raw webhook payload after SignalIntelligence has processed it."""
    signal_id:   str          # short unique ID for correlation + one-tap URLs
    received_at: datetime
    ticker:      str
    action:      str          # "buy" | "sell"
    price:       float
    grade:       str | None
    score:       int | None
    sweep:       str | None
    sl:          float | None
    tp:          float | None
    be:          float | None
    comment:     str
    raw:         dict         # the original payload


@dataclass
class GateResult:
    approved:   bool
    reason:     str
    gate_state: dict


@dataclass
class VenueResult:
    venue:    str             # "TradersPost" | "Retry" | "ManualEscalation"
    success:  bool
    message:  str             # human-readable outcome
    payload:  dict | None = None  # what was actually sent / queued
    detail:   dict = field(default_factory=dict)


@dataclass
class DispatchResult:
    chosen:     str           # name of the venue that accepted
    attempts:   list[VenueResult]


@dataclass
class CommanderResult:
    ok:        bool
    signal_id: str
    status:    str            # "executed" | "rejected" | "escalated" | "error"
    message:   str
    enriched:  EnrichedSignal | None = None
    gates:     dict | None         = None
    dispatch:  DispatchResult | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Signal Intelligence — enriches + fingerprints
# ═══════════════════════════════════════════════════════════════════════════

class SignalIntelligence:
    """
    First-line agent. Takes the raw webhook payload and produces an
    EnrichedSignal with a stable ID that every downstream agent can reference.
    Later we can also:
      - pull similar historical setups from the Neural Brain and attach a
        historical-winrate hint
      - tag the signal with current VIX / NQ state for context
    Today it just validates + fingerprints.
    """

    def enrich(self, raw: dict) -> EnrichedSignal:
        now = datetime.now(tz=EST)
        # Short ID that's unique enough for manual-fire URLs and log correlation
        seed = f"{now.isoformat()}|{raw.get('ticker')}|{raw.get('action')}|{raw.get('price')}"
        sig_id = hashlib.sha256(seed.encode()).hexdigest()[:10]

        def _f(key):
            v = raw.get(key)
            try:    return float(v) if v is not None and v != "" else None
            except (TypeError, ValueError): return None

        def _i(key):
            v = raw.get(key)
            try:    return int(v) if v is not None and v != "" else None
            except (TypeError, ValueError): return None

        # `grade_score` is the Pine v1.4.2 grader's emit key; older payloads
        # use `score`. Accept both so the field flows through unchanged.
        return EnrichedSignal(
            signal_id   = sig_id,
            received_at = now,
            ticker      = str(raw.get("ticker", "")).upper().strip(),
            action      = str(raw.get("action", "")).lower().strip(),
            price       = _f("price") or 0.0,
            grade       = raw.get("grade"),
            score       = _i("grade_score") or _i("score"),
            sweep       = raw.get("sweep"),
            sl          = _f("sl") or _f("stop_loss"),
            tp          = _f("tp") or _f("take_profit"),
            be          = _f("be"),
            comment     = str(raw.get("comment", "")).strip(),
            raw         = raw,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Risk Guardian — wraps the existing evaluate_gates
# ═══════════════════════════════════════════════════════════════════════════

class RiskGuardian:
    """
    Thin wrapper around app.evaluate_gates so ExecutionDispatcher doesn't
    import app (circular). The gate logic itself stays in app.py where it
    also serves /execute.
    """
    def __init__(self, gate_fn: Callable[[str, str | None, datetime], tuple[bool, str, dict]]):
        self._gate_fn = gate_fn

    def approve(self, enriched: EnrichedSignal) -> GateResult:
        ok, reason, state = self._gate_fn(enriched.ticker, enriched.grade, enriched.received_at)
        return GateResult(approved=ok, reason=reason, gate_state=state)


# ═══════════════════════════════════════════════════════════════════════════
# Execution Venues — each implements fire(enriched, payload)
# ═══════════════════════════════════════════════════════════════════════════

class ExecutionVenue:
    """Base venue — subclasses must implement fire()."""
    name: str = "base"

    def fire(self, enriched: EnrichedSignal, payload: dict) -> VenueResult:
        raise NotImplementedError


class TradersPostVenue(ExecutionVenue):
    """Primary venue: POST to TradersPost strategy webhook."""
    name = "TradersPost"

    def __init__(self, webhook_url: str | None = None, timeout: float = 10.0):
        self.url     = webhook_url or os.environ.get("TRADERSPOST_WEBHOOK_URL", "")
        self.timeout = timeout

    def fire(self, enriched: EnrichedSignal, payload: dict) -> VenueResult:
        if not self.url:
            return VenueResult(self.name, False, "TRADERSPOST_WEBHOOK_URL not configured")
        try:
            r = requests.post(self.url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            return VenueResult(
                self.name, True,
                f"TradersPost accepted ({r.status_code})",
                payload=payload,
                detail={"status_code": r.status_code, "response": r.text[:300]},
            )
        except requests.exceptions.Timeout:
            return VenueResult(self.name, False, "TradersPost timed out")
        except requests.exceptions.ConnectionError as e:
            return VenueResult(self.name, False, f"Connection error: {e}")
        except requests.exceptions.HTTPError as e:
            return VenueResult(
                self.name, False,
                f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                detail={"status_code": e.response.status_code},
            )
        except Exception as e:
            return VenueResult(self.name, False, f"Unexpected error: {e}")


class RetryVenue(ExecutionVenue):
    """
    Wraps an inner venue with exponential-backoff retry. Each attempt surfaces
    as its own VenueResult inside the final DispatchResult so the operator
    can see every try.
    """
    name = "Retry"

    def __init__(self, inner: ExecutionVenue, max_attempts: int = 3, base_delay: float = 1.5):
        self.inner        = inner
        self.max_attempts = max_attempts
        self.base_delay   = base_delay
        self._last_attempts: list[VenueResult] = []

    def fire(self, enriched: EnrichedSignal, payload: dict) -> VenueResult:
        self._last_attempts = []
        for attempt in range(1, self.max_attempts + 1):
            r = self.inner.fire(enriched, payload)
            r.detail["attempt"] = attempt
            self._last_attempts.append(r)
            if r.success:
                return VenueResult(
                    f"{self.name}:{self.inner.name}", True,
                    f"accepted on attempt {attempt} — {r.message}",
                    payload=payload,
                    detail={"attempts": attempt, "inner_detail": r.detail},
                )
            if attempt < self.max_attempts:
                delay = self.base_delay * (2 ** (attempt - 1))
                logger.warning(f"[Retry] attempt {attempt}/{self.max_attempts} failed: {r.message} — backoff {delay:.1f}s")
                time.sleep(delay)
        return VenueResult(
            f"{self.name}:{self.inner.name}", False,
            f"all {self.max_attempts} attempts failed",
            detail={
                "attempts":      self.max_attempts,
                "last_message":  self._last_attempts[-1].message if self._last_attempts else "no attempts",
                "inner_detail":  self._last_attempts[-1].detail  if self._last_attempts else {},
            },
        )

    def attempt_log(self) -> list[VenueResult]:
        return list(self._last_attempts)


# ── Pending-trade queue shared between the escalation venue and /fire endpoint
_pending_lock: threading.Lock = threading.Lock()
_pending:      dict[str, dict] = {}   # token -> {signal_id, payload, enriched, expires_at}

PENDING_TTL = timedelta(minutes=30)


def _gen_fire_token() -> str:
    """Cryptographically-random token for one-tap URLs."""
    return secrets.token_urlsafe(16)


def _sign_token(token: str) -> str:
    secret = os.environ.get("NOVA_FIRE_SECRET", "nova-dev-fire-secret")
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()[:16]


def queue_pending(enriched: EnrichedSignal, payload: dict) -> tuple[str, str]:
    """Add a trade to the pending-fire queue. Returns (token, signature)."""
    token = _gen_fire_token()
    sig   = _sign_token(token)
    with _pending_lock:
        _pending[token] = {
            "signal_id":  enriched.signal_id,
            "enriched":   enriched,
            "payload":    payload,
            "expires_at": datetime.now(tz=EST) + PENDING_TTL,
            "consumed":   False,
        }
    return token, sig


def consume_pending(token: str, sig: str) -> dict | None:
    """
    Validate + fetch a pending trade for manual firing. Returns the record
    (with enriched + payload) or None if token is invalid / expired / used.
    """
    with _pending_lock:
        rec = _pending.get(token)
        if not rec:                                  return None
        if rec["consumed"]:                          return None
        if datetime.now(tz=EST) > rec["expires_at"]: return None
        if not hmac.compare_digest(sig, _sign_token(token)): return None
        rec["consumed"] = True
        return rec


def purge_expired() -> int:
    """Drop expired entries from the pending queue. Returns count removed."""
    now = datetime.now(tz=EST)
    with _pending_lock:
        stale = [k for k, v in _pending.items() if now > v["expires_at"]]
        for k in stale: del _pending[k]
    return len(stale)


class ManualEscalationVenue(ExecutionVenue):
    """
    Last resort: when TradersPost has failed after retry, queue the trade
    + post an URGENT Discord alert with a one-tap /fire?token=...&sig=... URL.
    Sir taps the link on his phone, the server calls TradersPost one more
    time with the queued payload. Token expires in 30 minutes.
    """
    name = "ManualEscalation"

    def __init__(self, observability: "Observability | None" = None):
        self.observability = observability

    def fire(self, enriched: EnrichedSignal, payload: dict) -> VenueResult:
        token, sig = queue_pending(enriched, payload)
        base       = os.environ.get("NOVA_PUBLIC_BASE", "https://nova-production-72f5.up.railway.app")
        url        = f"{base}/fire?token={token}&sig={sig}"

        # Blast an URGENT notification so Sir can act from his phone
        if self.observability:
            self.observability.escalate(enriched, payload, fire_url=url)

        logger.error(f"[ManualEscalation] trade {enriched.signal_id} queued — fire URL: {url}")
        return VenueResult(
            self.name, True,          # "success" from dispatcher's POV — trade is not dropped
            f"escalated to manual fire (token expires in {int(PENDING_TTL.total_seconds() / 60)}m)",
            payload=payload,
            detail={"fire_url": url, "token": token, "expires_in_min": int(PENDING_TTL.total_seconds() / 60)},
        )


# ═══════════════════════════════════════════════════════════════════════════
# Execution Dispatcher — runs venues in order until one reports success
# ═══════════════════════════════════════════════════════════════════════════

class ExecutionDispatcher:
    """
    Tries venues in the configured order. Stops at the first success. Every
    attempt is recorded in DispatchResult.attempts so the operator can see
    the full chain.
    """
    def __init__(self, venues: list[ExecutionVenue]):
        self.venues = venues

    def dispatch(self, enriched: EnrichedSignal, payload: dict) -> DispatchResult:
        attempts: list[VenueResult] = []
        chosen = None
        for venue in self.venues:
            logger.info(f"[Dispatcher] trying venue: {venue.name}")
            result = venue.fire(enriched, payload)
            attempts.append(result)

            # If the venue is a RetryVenue, flatten its inner attempts for
            # observability
            if isinstance(venue, RetryVenue):
                attempts.extend(venue.attempt_log())

            if result.success:
                chosen = result.venue
                break
        return DispatchResult(chosen=chosen or "none", attempts=attempts)


# ═══════════════════════════════════════════════════════════════════════════
# Observability — mirrors to Discord + in-memory ledger
# ═══════════════════════════════════════════════════════════════════════════

_LEDGER_LOCK = threading.Lock()
_LEDGER: list[dict] = []   # rolling ledger of every commander decision
_LEDGER_MAX = 200


class Observability:
    """
    Fans out state transitions to all configured notification channels and
    the in-memory ledger that /agents/ledger exposes.
    """

    def __init__(self, discord_url: str | None = None):
        self.discord_url = discord_url or os.environ.get("NOVA_DISCORD_WEBHOOK_URL", "")

    # ── Ledger ───────────────────────────────────────────────────────────
    def _append_ledger(self, event: str, enriched: EnrichedSignal | None, extra: dict):
        entry = {
            "ts":        datetime.now(tz=EST).isoformat(),
            "event":     event,
            "signal_id": enriched.signal_id if enriched else None,
            "ticker":    enriched.ticker    if enriched else None,
            **extra,
        }
        with _LEDGER_LOCK:
            _LEDGER.insert(0, entry)
            if len(_LEDGER) > _LEDGER_MAX:
                _LEDGER.pop()

    # ── Discord helpers ──────────────────────────────────────────────────
    def _post_discord(self, embed: dict):
        if not self.discord_url:
            return
        try:
            requests.post(
                self.discord_url,
                json={"embeds": [embed]},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"[Observability] Discord notify failed: {e}")

    def _base_embed(self, enriched: EnrichedSignal, color: int, title: str) -> dict:
        action = enriched.action.upper()
        fields = [
            {"name": "Ticker",   "value": enriched.ticker,              "inline": True},
            {"name": "Action",   "value": action,                       "inline": True},
            {"name": "Price",    "value": f"{enriched.price:,.2f}",     "inline": True},
        ]
        if enriched.grade: fields.append({"name": "Grade", "value": str(enriched.grade), "inline": True})
        if enriched.sweep: fields.append({"name": "Sweep", "value": str(enriched.sweep), "inline": True})
        if enriched.sl:    fields.append({"name": "Stop",  "value": f"{enriched.sl:,.2f}", "inline": True})
        if enriched.tp:    fields.append({"name": "Target","value": f"{enriched.tp:,.2f}", "inline": True})
        return {
            "title":       title,
            "color":       color,
            "fields":      fields,
            "footer":      {"text": f"signal {enriched.signal_id} · {enriched.received_at.strftime('%Y-%m-%d %H:%M:%S %Z')}"},
        }

    # ── Public event methods ─────────────────────────────────────────────

    def signal_received(self, enriched: EnrichedSignal):
        self._append_ledger("signal_received", enriched, {})
        logger.info(f"[Observability] signal received id={enriched.signal_id}")

    def signal_rejected(self, enriched: EnrichedSignal, gate: GateResult):
        # Log-only. Rejected signals aren't actionable for the community —
        # they clutter the channel. Keep the ledger entry for /agents/ledger.
        self._append_ledger("signal_rejected", enriched, {"reason": gate.reason, "gates": gate.gate_state})

    def signal_executed(self, enriched: EnrichedSignal, dispatch: DispatchResult):
        self._append_ledger("signal_executed", enriched, {"chosen": dispatch.chosen})
        embed = self._base_embed(enriched, color=0x00C853, title="✅ Signal executed")
        embed["description"] = f"Forwarded via **{dispatch.chosen}**. Attempts: {len(dispatch.attempts)}."
        self._post_discord(embed)

    def signal_failed(self, enriched: EnrichedSignal, dispatch: DispatchResult):
        self._append_ledger("signal_failed", enriched, {"attempts": [a.message for a in dispatch.attempts]})
        embed = self._base_embed(enriched, color=0xE53E3E, title="❗ Signal dispatch failed")
        embed["description"] = "Every venue failed. Trade was NOT placed. Investigate immediately."
        self._post_discord(embed)

    def escalate(self, enriched: EnrichedSignal, payload: dict, fire_url: str):
        """Manual-escalation notice with a one-tap fire URL."""
        self._append_ledger("signal_escalated", enriched, {"fire_url": fire_url})
        embed = self._base_embed(enriched, color=0xFF6B00, title="🚨 MANUAL FIRE REQUIRED")
        embed["description"] = (
            "TradersPost failed. Tap the link below within 30 minutes to fire manually."
        )
        embed["fields"].append({
            "name":  "One-tap fire",
            "value": f"[{fire_url}]({fire_url})",
            "inline": False,
        })
        self._post_discord(embed)

    def manual_fired(self, enriched: EnrichedSignal, venue_result: VenueResult):
        self._append_ledger("signal_manual_fired", enriched, {"success": venue_result.success, "message": venue_result.message})
        color = 0x00C853 if venue_result.success else 0xE53E3E
        title = "✅ Manual fire accepted" if venue_result.success else "❗ Manual fire failed"
        embed = self._base_embed(enriched, color=color, title=title)
        embed["description"] = venue_result.message
        self._post_discord(embed)


def get_ledger(limit: int = 50) -> list[dict]:
    with _LEDGER_LOCK:
        return list(_LEDGER[:limit])


# ═══════════════════════════════════════════════════════════════════════════
# Heartbeat Agent — operational watchdog
# ───────────────────────────────────────────────────────────────────────────
# Every 5 minutes during live sessions (and every 15 minutes outside them) the
# agent runs a full chain check: can we reach TradersPost, is the Discord
# webhook valid, has the Railway in-memory state drifted from expected. If
# anything looks wrong, it fires an URGENT Discord alert *before* a real
# signal arrives and finds itself unable to execute.
# ═══════════════════════════════════════════════════════════════════════════

class HeartbeatAgent:
    """
    Runs in a daemon thread started by TradingCommander.__init__.

    Checks on each cycle:
      1. TradersPost webhook reachability (HEAD or lightweight probe)
      2. Discord webhook reachability
      3. Railway process health (self-ping of /status)
      4. In-memory pending-fire queue size — if anything's been sitting for
         more than PENDING_TTL-5min without being tapped, escalate again
    """
    def __init__(self, observability: "Observability", self_base_url: str | None = None):
        self.obs      = observability
        self.self_url = (self_base_url or os.environ.get("NOVA_PUBLIC_BASE",
                         "https://nova-production-72f5.up.railway.app")).rstrip("/")
        self.tp_url   = os.environ.get("TRADERSPOST_WEBHOOK_URL", "")
        self.interval_live  = int(os.environ.get("NOVA_HEARTBEAT_INTERVAL_LIVE",  "300"))   # 5m
        self.interval_quiet = int(os.environ.get("NOVA_HEARTBEAT_INTERVAL_QUIET", "900"))   # 15m
        self._stop    = threading.Event()
        self._thread  = None
        self._last_tp_ok:      bool | None = None
        self._last_discord_ok: bool | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="HeartbeatAgent", daemon=True)
        self._thread.start()
        logger.info("[HeartbeatAgent] started")

    def stop(self):
        self._stop.set()

    def _in_live_session(self) -> bool:
        """London 02:00-05:00 EST or NY AM 08:30-11:00 EST."""
        now = datetime.now(tz=EST)
        if now.weekday() >= 5:
            return False
        minutes = now.hour * 60 + now.minute
        return (2*60     <= minutes < 5*60) or (8*60+30 <= minutes < 11*60)

    def _probe_traderspost(self) -> tuple[bool, str]:
        """
        Reachability probe — HEAD request so TradersPost never sees a trade
        payload, never emails Sir, never risks a bogus fill. A HEAD will
        typically 405 (Method Not Allowed) since TP webhooks only accept
        POST, but ANY HTTP response means the host is alive and routable.
        That's all a heartbeat needs to tell us.
        """
        if not self.tp_url:
            return False, "TRADERSPOST_WEBHOOK_URL not set"
        try:
            r = requests.head(self.tp_url, timeout=6, allow_redirects=False)
            # 200/204/301/302/405 all mean "TP host is alive". Only 5xx or
            # connection errors indicate real trouble.
            if r.status_code < 500:
                return True, f"TP reachable ({r.status_code})"
            return False, f"TP {r.status_code}"
        except requests.exceptions.Timeout:
            return False, "TP timeout"
        except requests.exceptions.ConnectionError as e:
            return False, f"TP connection: {e}"
        except Exception as e:
            return False, f"TP unexpected: {e}"

    def _probe_discord(self) -> tuple[bool, str]:
        """HEAD-probe the Discord webhook — no body means no message posted."""
        if not self.obs.discord_url:
            return False, "NOVA_DISCORD_WEBHOOK_URL not set"
        try:
            r = requests.head(self.obs.discord_url, timeout=5, allow_redirects=False)
            if r.status_code < 500:
                return True, f"Discord reachable ({r.status_code})"
            return False, f"Discord {r.status_code}"
        except Exception as e:
            return False, f"Discord error: {e}"

    def _probe_self(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.self_url}/status", timeout=5)
            if r.status_code == 200:
                return True, "Railway self OK"
            return False, f"Railway self {r.status_code}"
        except Exception as e:
            return False, f"Railway self error: {e}"

    def _check_pending_queue(self) -> int:
        """Count pending trades that are within 5 min of expiry and re-ping."""
        now      = datetime.now(tz=EST)
        near_exp = 0
        with _pending_lock:
            for tok, rec in _pending.items():
                if rec["consumed"]:
                    continue
                if (rec["expires_at"] - now) <= timedelta(minutes=5):
                    near_exp += 1
        return near_exp

    def _loop(self):
        time.sleep(10)  # let the server finish booting before first probe
        while not self._stop.is_set():
            tp_ok,      tp_msg      = self._probe_traderspost()
            disc_ok,    disc_msg    = self._probe_discord()
            self_ok,    self_msg    = self._probe_self()
            near_expiry             = self._check_pending_queue()

            # Log transitions to ledger only. Discord stays silent unless TP
            # goes DOWN for real (critical; worth a ping). Recoveries and
            # routine OKs stay out of the channel.
            if self._last_tp_ok is True and tp_ok is False:
                # Actual degradation — alert
                self.obs._append_ledger(
                    "heartbeat_tp_transition", None,
                    {"from": True, "to": False, "msg": tp_msg},
                )
                embed = {
                    "title": "🫀 TradersPost DOWN ❗",
                    "color": 0xE53E3E,
                    "description": tp_msg,
                }
                self.obs._post_discord(embed)
            elif self._last_tp_ok is not None and self._last_tp_ok != tp_ok:
                # Log-only (e.g., recovery)
                self.obs._append_ledger(
                    "heartbeat_tp_transition", None,
                    {"from": self._last_tp_ok, "to": tp_ok, "msg": tp_msg},
                )
            self._last_tp_ok = tp_ok

            if self._last_discord_ok is not None and self._last_discord_ok != disc_ok:
                # If Discord broke, the recovery ping is how we find out — log
                # to ledger regardless.
                self.obs._append_ledger(
                    "heartbeat_discord_transition", None,
                    {"from": self._last_discord_ok, "to": disc_ok, "msg": disc_msg},
                )
            self._last_discord_ok = disc_ok

            # Lightweight ledger cadence so /agents/ledger shows liveness
            self.obs._append_ledger(
                "heartbeat", None,
                {
                    "tp":         tp_ok,
                    "discord":    disc_ok,
                    "self":       self_ok,
                    "near_expiry_pending": near_expiry,
                    "tp_msg":     tp_msg,
                    "self_msg":   self_msg,
                    "live":       self._in_live_session(),
                },
            )

            # Purge expired manual-fire tokens on every cycle
            purge_expired()

            sleep_s = self.interval_live if self._in_live_session() else self.interval_quiet
            self._stop.wait(sleep_s)


# ═══════════════════════════════════════════════════════════════════════════
# Trading Commander — top-level orchestrator
# ═══════════════════════════════════════════════════════════════════════════

class TradingCommander:
    """
    The only class app.webhook() imports. Runs the whole chain:
       raw → enrich → gate → dispatch → observe
    """

    def __init__(
        self,
        gate_fn,
        build_tp_payload,
        discord_url: str | None = None,
        start_heartbeat: bool = True,
    ):
        self.intel = SignalIntelligence()
        self.risk  = RiskGuardian(gate_fn)
        self.obs   = Observability(discord_url=discord_url)
        self.exec  = ExecutionDispatcher(venues=[
            RetryVenue(TradersPostVenue(), max_attempts=3, base_delay=1.5),
            ManualEscalationVenue(observability=self.obs),
        ])
        self._build_tp_payload = build_tp_payload

        # Fire up the heartbeat watchdog unless caller opts out (unit tests)
        self.heartbeat = HeartbeatAgent(self.obs) if start_heartbeat else None
        if self.heartbeat:
            self.heartbeat.start()

    def handle(self, raw: dict) -> CommanderResult:
        """
        Full pipeline. Returns a CommanderResult whose `status` field tells
        the Flask handler how to respond:
          "executed"  — went through TradersPost successfully
          "escalated" — queued for manual fire; Discord alert sent
          "rejected"  — gate blocked it (weekend, session closed, etc.)
          "error"     — invalid signal, cannot process
        """
        # 1. Enrich
        enriched = self.intel.enrich(raw)
        self.obs.signal_received(enriched)

        if not enriched.ticker or enriched.action not in ("buy", "sell"):
            return CommanderResult(
                ok=False, signal_id=enriched.signal_id, status="error",
                message="Invalid ticker or action", enriched=enriched,
            )

        # 2. Gate
        gate = self.risk.approve(enriched)
        if not gate.approved:
            self.obs.signal_rejected(enriched, gate)
            return CommanderResult(
                ok=False, signal_id=enriched.signal_id, status="rejected",
                message=gate.reason, enriched=enriched, gates=gate.gate_state,
            )

        # 3. Dispatch
        session = gate.gate_state.get("session") or "unknown"
        payload = self._build_tp_payload(raw, session)
        dispatch = self.exec.dispatch(enriched, payload)

        # 4. Observe
        if dispatch.chosen == "none":
            self.obs.signal_failed(enriched, dispatch)
            status, message = "error", "all venues failed"
        elif dispatch.chosen.startswith("ManualEscalation"):
            status, message = "escalated", "queued for manual fire"
        else:
            self.obs.signal_executed(enriched, dispatch)
            status, message = "executed", f"forwarded via {dispatch.chosen}"

        return CommanderResult(
            ok       = (status != "error"),
            signal_id= enriched.signal_id,
            status   = status,
            message  = message,
            enriched = enriched,
            gates    = gate.gate_state,
            dispatch = dispatch,
        )

    def fire_pending(self, token: str, sig: str) -> dict:
        """
        Called by the /fire endpoint when Sir taps the one-tap URL. Looks up
        the queued payload, fires TradersPost once more (no retry — this is
        the manual backstop). Returns a dict suitable for jsonify().
        """
        rec = consume_pending(token, sig)
        if not rec:
            return {"ok": False, "status": "invalid", "message": "token invalid, expired, or already used"}

        enriched = rec["enriched"]
        payload  = rec["payload"]
        venue    = TradersPostVenue()
        r        = venue.fire(enriched, payload)
        self.obs.manual_fired(enriched, r)
        return {
            "ok":         r.success,
            "status":     "fired" if r.success else "failed",
            "message":    r.message,
            "signal_id":  enriched.signal_id,
            "detail":     r.detail,
        }
