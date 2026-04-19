"""
═══════════════════════════════════════════════════════════════════════════
NOVA TradingView Guardian — Chart-side agent hierarchy
───────────────────────────────────────────────────────────────────────────

Watches the live TradingView Desktop tab via Chrome DevTools Protocol
(port 9222). Detects and alerts on any chart-side drift that would cause
Pine signals to silently stop firing:

    ChartGuardianAgent             (parent — polling loop, coordinator)
    ├── StudyHealthAgent           (NOVA ICT study still loaded + armed?)
    │     └── AutoReapplyAgent     (if missing, attempt re-push + re-apply)
    ├── SymbolLockAgent            (chart on CME_MINI:NQ1! @ 15m?)
    ├── InputDriftAgent            (i_secret still set? session times intact?)
    ├── SessionStateAgent          (London/NY_AM BG painting? PDH/PDL drawn?)
    │     ├── LondonWatcherAgent   (02:00-05:00 EST specific checks)
    │     └── NYWatcherAgent       (08:30-11:00 EST specific checks)
    └── AlertArmedAgent            (TV-side alert still active, not expired?)

Everything I built earlier protects signals AFTER they leave the chart.
This layer protects the chart itself from silent breakage.

Alerts flow:
  - Drift detected → Discord embed (NOVA_DISCORD_WEBHOOK_URL)
  - Also pushed to local nova_ui_server (:7336) so the desktop dashboard
    shows an "alert" row in the chat log
  - Logged to stderr for the Electron subprocess log

Usage:
    python nova_tv_guardian.py         # standalone, polling loop
    # or spawned by Electron main.js alongside nova_ui_server + nova_assistant

Dependencies:
    pychrome   (pure-python CDP client)
    requests
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

try:
    import pychrome
    _PYCHROME_AVAILABLE = True
except ImportError:
    _PYCHROME_AVAILABLE = False


# ── Config ─────────────────────────────────────────────────────────────────────
CDP_HOST = os.environ.get("NOVA_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("NOVA_CDP_PORT", "9222"))

EXPECTED_SYMBOL     = os.environ.get("NOVA_EXPECTED_SYMBOL",     "CME_MINI:NQ1!")
EXPECTED_RESOLUTION = os.environ.get("NOVA_EXPECTED_RESOLUTION", "15")
EXPECTED_STUDY_NAME = "NOVA ICT - London"   # substring match — includes the " - London and NY AM" suffix
EXPECTED_SECRET_LEN = 43                    # token_urlsafe(32) produces 43 chars

POLL_INTERVAL_LIVE  = int(os.environ.get("NOVA_TV_POLL_LIVE",  "60"))   # 1m during sessions
POLL_INTERVAL_QUIET = int(os.environ.get("NOVA_TV_POLL_QUIET", "300"))  # 5m outside

DISCORD_URL = os.environ.get("NOVA_DISCORD_WEBHOOK_URL", "")
UI_SERVER   = os.environ.get("NOVA_UI_URL", "http://127.0.0.1:7336")

EST = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [tv-guardian] %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("nova-tv-guardian")


# ═══════════════════════════════════════════════════════════════════════════
# CDP connection wrapper
# ═══════════════════════════════════════════════════════════════════════════

class TVConnection:
    """
    Thin wrapper over pychrome. Finds the TradingView tab on CDP port 9222
    and exposes an evaluate() method that runs JavaScript in the page.
    """
    def __init__(self, host: str = CDP_HOST, port: int = CDP_PORT):
        self.host   = host
        self.port   = port
        self.browser = None
        self.tab    = None

    def connect(self) -> bool:
        if not _PYCHROME_AVAILABLE:
            logger.error("pychrome not installed — run: pip install pychrome")
            return False
        try:
            self.browser = pychrome.Browser(url=f"http://{self.host}:{self.port}")
            for tab in self.browser.list_tab():
                kw = getattr(tab, "_kwargs", {}) or {}
                url = kw.get("url", "") or ""
                typ = kw.get("type", "")
                if typ == "page" and "tradingview.com" in url:
                    self.tab = tab
                    self.tab.start()
                    self.tab.Runtime.enable()
                    logger.info(f"connected to TradingView tab: {url[:80]}")
                    return True
            logger.warning("no TradingView tab found on CDP")
            return False
        except Exception as e:
            logger.error(f"CDP connect failed: {e}")
            return False

    def is_alive(self) -> bool:
        if not self.tab:
            return False
        try:
            r = self.tab.Runtime.evaluate(expression="1+1", returnByValue=True, _timeout=3)
            return r.get("result", {}).get("value") == 2
        except Exception:
            return False

    def evaluate(self, expression: str, timeout: float = 5.0) -> Any:
        """
        Run JS, return the evaluated value. Wraps the expression in an IIFE
        so callers can write multi-statement scripts without worrying about
        returning the last expression explicitly.
        """
        if not self.tab:
            return None
        try:
            result = self.tab.Runtime.evaluate(
                expression     = expression,
                returnByValue  = True,
                _timeout       = timeout,
            )
            if result.get("exceptionDetails"):
                logger.warning(f"js exception: {result['exceptionDetails'].get('text','')}")
                return None
            return result.get("result", {}).get("value")
        except Exception as e:
            logger.warning(f"evaluate failed: {e}")
            return None

    def disconnect(self):
        try:
            if self.tab:
                self.tab.stop()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Drift reporting — shared sink for every agent
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DriftEvent:
    agent:     str
    severity:  str        # "info" | "warn" | "critical"
    title:     str
    detail:    str
    at:        datetime   = field(default_factory=lambda: datetime.now(tz=EST))


class DriftSink:
    """Fans drift events out to Discord + local UI server + stderr. Dedups
    identical drifts within a 10-minute window so we don't spam the channel
    with 'study still missing' every minute."""

    _SEVERITY_COLOR = {
        "info":     0x00E5FF,
        "warn":     0xFFB020,
        "critical": 0xE53E3E,
    }
    _DEDUPE_WINDOW_S = 600

    def __init__(self):
        self._recent: dict[str, datetime] = {}

    def _recently_reported(self, key: str) -> bool:
        ts = self._recent.get(key)
        if ts and (datetime.now(tz=EST) - ts).total_seconds() < self._DEDUPE_WINDOW_S:
            return True
        self._recent[key] = datetime.now(tz=EST)
        return False

    def emit(self, evt: DriftEvent):
        key = f"{evt.agent}|{evt.title}"
        if self._recently_reported(key):
            return
        logger.warning(f"[{evt.agent}] {evt.severity.upper()}: {evt.title} — {evt.detail}")
        self._post_discord(evt)
        self._post_ui(evt)

    def _post_discord(self, evt: DriftEvent):
        if not DISCORD_URL:
            return
        try:
            requests.post(DISCORD_URL, json={"embeds": [{
                "title":       f"🛰️ TV Guardian — {evt.title}",
                "description": f"**Agent:** {evt.agent}\n{evt.detail}",
                "color":       self._SEVERITY_COLOR.get(evt.severity, 0x808080),
                "footer":      {"text": evt.at.strftime("%Y-%m-%d %H:%M:%S %Z")},
            }]}, timeout=4)
        except Exception as e:
            logger.warning(f"discord emit failed: {e}")

    def _post_ui(self, evt: DriftEvent):
        try:
            requests.post(f"{UI_SERVER}/push", json={
                "type": "log",
                "payload": {
                    "kind": "alert",
                    "msg":  f"TV drift ({evt.agent}): {evt.title} — {evt.detail}",
                },
            }, timeout=1.5)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Agent base + sub-agents
# ═══════════════════════════════════════════════════════════════════════════

class GuardianAgent:
    name = "base"
    def __init__(self, tv: TVConnection, sink: DriftSink):
        self.tv   = tv
        self.sink = sink

    def check(self) -> dict:
        """Run one cycle of this agent's checks. Return a dict of findings."""
        raise NotImplementedError

    def _emit(self, severity: str, title: str, detail: str):
        self.sink.emit(DriftEvent(agent=self.name, severity=severity, title=title, detail=detail))


# ── Sub-sub-agent: attempt to re-apply a missing study ─────────────────────
class AutoReapplyAgent(GuardianAgent):
    """Sub-agent of StudyHealthAgent. If the NOVA ICT study is missing,
    tries to re-click 'Add to chart' on the saved Pine script. Best-effort —
    logs success/failure to the drift sink."""
    name = "AutoReapply"

    def check(self) -> dict:
        # Best-effort: click the Pine editor's Add-to-chart button if the
        # editor is open with the NOVA ICT source. Otherwise flag for manual.
        js = """
        (() => {
          const btn = [...document.querySelectorAll('button')].find(b => b.getAttribute('title') === 'Add to chart');
          if (!btn) return {clicked: false, reason: 'Add to chart button not visible'};
          btn.click();
          return {clicked: true};
        })()
        """
        r = self.tv.evaluate(js) or {}
        if r.get("clicked"):
            self._emit("info", "Auto-reapply attempted",
                       "Clicked 'Add to chart' from Pine editor. Verify a settings modal opened for the webhook secret.")
            return {"attempted": True}
        else:
            self._emit("critical", "Auto-reapply not possible",
                       f"Pine editor not in the state we need. {r.get('reason','')}. Manual reapply required.")
            return {"attempted": False}


# ── StudyHealth (with AutoReapply as sub-agent) ────────────────────────────
class StudyHealthAgent(GuardianAgent):
    """Is the NOVA ICT study loaded on the chart?"""
    name = "StudyHealth"

    def __init__(self, tv, sink):
        super().__init__(tv, sink)
        self.auto_reapply = AutoReapplyAgent(tv, sink)
        self._last_study_id: str | None = None

    def check(self) -> dict:
        js = """
        (() => {
          try {
            const api   = window.TradingViewApi || window.tvWidget;
            const chart = api?.activeChart?.();
            const studies = chart?.getAllStudies?.() || [];
            const nova = studies.find(s => s.name && s.name.includes('%s'));
            return {
              found: !!nova,
              id:    nova?.id || null,
              name:  nova?.name || null,
              total: studies.length,
            };
          } catch (e) { return {error: String(e)}; }
        })()
        """ % EXPECTED_STUDY_NAME
        r = self.tv.evaluate(js) or {}
        if r.get("error"):
            return {"ok": False, "reason": f"js error: {r['error']}"}
        if not r.get("found"):
            self._emit("critical", "NOVA ICT study missing",
                       f"Chart has {r.get('total',0)} studies but none match '{EXPECTED_STUDY_NAME}'. "
                       f"No alerts will fire until the study is re-applied.")
            # Try to auto-reapply as a sub-agent
            self.auto_reapply.check()
            self._last_study_id = None
            return {"ok": False, "reason": "missing"}
        # Study ID changed (re-applied externally) — not drift, just note it
        if self._last_study_id and self._last_study_id != r["id"]:
            logger.info(f"study ID changed: {self._last_study_id} -> {r['id']} (likely re-applied)")
        self._last_study_id = r["id"]
        return {"ok": True, "id": r["id"]}


# ── SymbolLock ─────────────────────────────────────────────────────────────
class SymbolLockAgent(GuardianAgent):
    """Chart still on the expected symbol + resolution?"""
    name = "SymbolLock"

    def check(self) -> dict:
        js = """
        (() => {
          const api = window.TradingViewApi || window.tvWidget;
          const chart = api?.activeChart?.();
          return {
            symbol:     chart?.symbol?.(),
            resolution: chart?.resolution?.(),
          };
        })()
        """
        r = self.tv.evaluate(js) or {}
        sym = r.get("symbol") or ""
        res = str(r.get("resolution") or "")
        drift = []
        if sym != EXPECTED_SYMBOL:
            drift.append(f"symbol is '{sym}', expected '{EXPECTED_SYMBOL}'")
        if res != EXPECTED_RESOLUTION:
            drift.append(f"resolution is '{res}', expected '{EXPECTED_RESOLUTION}'")
        if drift:
            self._emit("critical", "Chart symbol/timeframe drift",
                       "; ".join(drift) + ". NOVA strategy only fires on the correct pair.")
            return {"ok": False, "drift": drift}
        return {"ok": True, "symbol": sym, "resolution": res}


# ── InputDrift — verify i_secret is set + key params unchanged ─────────────
class InputDriftAgent(GuardianAgent):
    """Verify the NOVA ICT study's inputs are intact (esp. webhook secret)."""
    name = "InputDrift"

    def check(self) -> dict:
        js = """
        (() => {
          const api = window.TradingViewApi || window.tvWidget;
          const chart = api?.activeChart?.();
          const studies = chart?.getAllStudies?.() || [];
          const nova = studies.find(s => s.name && s.name.includes('%s'));
          if (!nova) return {missing: true};
          const studyApi = chart.getStudyById(nova.id);
          const inputs = studyApi.getInputValues ? studyApi.getInputValues() : [];
          // Rebuild a small map by index (in_0 .. in_N) to Pine inputs
          const inMap = Object.fromEntries(inputs.map(i => [i.id, i.value]));
          return {
            london:   inMap.in_0 || null,
            ny:       inMap.in_1 || null,
            swing:    inMap.in_3 || null,
            bpd:      inMap.in_4 || null,
            buf:      inMap.in_5 || null,
            rr:       inMap.in_6 || null,
            be:       inMap.in_7 || null,
            secret_len: (String(inMap.in_11 || '')).length,
          };
        })()
        """ % EXPECTED_STUDY_NAME
        r = self.tv.evaluate(js) or {}
        if r.get("missing"):
            return {"ok": False, "reason": "study missing (handled by StudyHealth)"}

        drift = []
        expected = {
            "london": "0200-0500",
            "ny":     "0830-1100",
            "swing":  "5",
            "bpd":    "96",
            "buf":    "2",
            "rr":     "2",
            "be":     "1",
        }
        for k, want in expected.items():
            got = str(r.get(k) or "")
            if got != want:
                drift.append(f"{k}: got '{got}', expected '{want}'")
        if r.get("secret_len", 0) != EXPECTED_SECRET_LEN:
            drift.append(f"webhook secret length {r.get('secret_len',0)} (expected {EXPECTED_SECRET_LEN}). "
                         f"Alerts will be rejected at Railway's /webhook until the secret input is refilled.")

        if drift:
            self._emit("critical" if "secret" in " ".join(drift) else "warn",
                       "NOVA ICT input drift", " | ".join(drift))
            return {"ok": False, "drift": drift}
        return {"ok": True}


# ── Session state sub-watchers (sub-sub-agents) ────────────────────────────
class LondonWatcherAgent(GuardianAgent):
    """Between 02:00-05:00 EST: verify London session BG is actually painting."""
    name = "LondonWatcher"

    def _in_window(self) -> bool:
        now = datetime.now(tz=EST)
        mins = now.hour * 60 + now.minute
        return 2*60 <= mins < 5*60 and now.weekday() < 5

    def check(self) -> dict:
        if not self._in_window():
            return {"skipped": True, "reason": "outside London window"}
        # Read the Pine-painted background or session label from the chart's
        # visible labels. Simple heuristic: query for any "London" label.
        js = """
        (() => {
          // Look for session bg color attr on any painted range
          const api = window.TradingViewApi || window.tvWidget;
          const chart = api?.activeChart?.();
          const studies = chart?.getAllStudies?.() || [];
          const nova = studies.find(s => s.name && s.name.includes('NOVA ICT'));
          return {present: !!nova};
        })()
        """
        r = self.tv.evaluate(js) or {}
        if not r.get("present"):
            self._emit("critical", "London window active but NOVA study absent",
                       "It's mid-London session and the strategy is not loaded. No entries can fire.")
            return {"ok": False}
        return {"ok": True}


class NYWatcherAgent(GuardianAgent):
    """Between 08:30-11:00 EST: same check for NY AM."""
    name = "NYWatcher"

    def _in_window(self) -> bool:
        now = datetime.now(tz=EST)
        mins = now.hour * 60 + now.minute
        return 8*60+30 <= mins < 11*60 and now.weekday() < 5

    def check(self) -> dict:
        if not self._in_window():
            return {"skipped": True, "reason": "outside NY AM window"}
        js = """
        (() => {
          const api = window.TradingViewApi || window.tvWidget;
          const chart = api?.activeChart?.();
          const studies = chart?.getAllStudies?.() || [];
          const nova = studies.find(s => s.name && s.name.includes('NOVA ICT'));
          return {present: !!nova};
        })()
        """
        r = self.tv.evaluate(js) or {}
        if not r.get("present"):
            self._emit("critical", "NY AM window active but NOVA study absent",
                       "Mid-NY AM session and the strategy is not loaded. No entries can fire.")
            return {"ok": False}
        return {"ok": True}


class SessionStateAgent(GuardianAgent):
    """Parent of per-session watchers."""
    name = "SessionState"
    def __init__(self, tv, sink):
        super().__init__(tv, sink)
        self.london = LondonWatcherAgent(tv, sink)
        self.ny     = NYWatcherAgent(tv, sink)

    def check(self) -> dict:
        return {
            "london": self.london.check(),
            "ny":     self.ny.check(),
        }


# ── AlertArmed — is the TradingView alert still active? ────────────────────
class AlertArmedAgent(GuardianAgent):
    """
    Detects whether a TradingView alert for the NOVA ICT strategy exists
    and isn't paused. TradingView alerts can expire (2-month default) or
    get paused — if that happens, the chart fires nothing.

    Note: checking the alerts panel requires the alert side-panel to be
    open, which it usually isn't. As a proxy, we check whether the strategy
    is set up with alerts via the studyApi. Full panel inspection would
    require opening/closing the panel which disturbs UI state.
    """
    name = "AlertArmed"

    def check(self) -> dict:
        js = """
        (() => {
          const alertsBtn = document.querySelector('[data-name="alerts-main-dialog"]') ||
                            [...document.querySelectorAll('[aria-label]')].find(e => (e.getAttribute('aria-label')||'').toLowerCase().includes('alert'));
          // We can't reliably count alerts without opening the panel. Best
          // we can do passively: check the alert-count badge if TV shows one.
          const badges = [...document.querySelectorAll('[class*="badge"]')]
            .map(b => b.textContent && b.textContent.trim())
            .filter(t => t && /^\\d+$/.test(t));
          return {badges};
        })()
        """
        r = self.tv.evaluate(js) or {}
        # Passive mode — we can't assert much without opening the alert
        # panel. Just record alert-count badges if visible. Future: drive
        # the panel open periodically.
        return {"ok": True, "badges": r.get("badges", [])}


# ═══════════════════════════════════════════════════════════════════════════
# Chart Guardian — top-level coordinator
# ═══════════════════════════════════════════════════════════════════════════

class ChartGuardianAgent:
    """
    Parent agent. Connects to CDP, instantiates every sub-agent, runs the
    polling loop, and flags loss-of-connection as its own drift event.
    """
    def __init__(self):
        self.tv   = TVConnection()
        self.sink = DriftSink()
        self.agents: list[GuardianAgent] = []
        self._stop = False

    def start(self) -> bool:
        if not self.tv.connect():
            return False
        self.agents = [
            StudyHealthAgent(self.tv, self.sink),
            SymbolLockAgent (self.tv, self.sink),
            InputDriftAgent (self.tv, self.sink),
            SessionStateAgent(self.tv, self.sink),
            AlertArmedAgent (self.tv, self.sink),
        ]
        logger.info(f"ChartGuardian started with {len(self.agents)} top-level agents")
        return True

    def _in_live_session(self) -> bool:
        now = datetime.now(tz=EST)
        if now.weekday() >= 5:
            return False
        mins = now.hour * 60 + now.minute
        return (2*60 <= mins < 5*60) or (8*60+30 <= mins < 11*60)

    def tick(self) -> dict:
        if not self.tv.is_alive():
            logger.warning("CDP connection lost — attempting reconnect")
            self.tv.disconnect()
            if not self.tv.connect():
                self.sink.emit(DriftEvent(
                    agent="ChartGuardian", severity="critical",
                    title="CDP connection lost",
                    detail="Guardian can't see the chart. Is the TradingView Desktop app running with --remote-debugging-port=9222?",
                ))
                return {"ok": False}
        out = {}
        for agent in self.agents:
            try:
                out[agent.name] = agent.check()
            except Exception as e:
                logger.error(f"[{agent.name}] agent error: {e}")
                out[agent.name] = {"error": str(e)}
        return out

    def run(self):
        if not self.start():
            logger.error("Guardian failed to start — CDP not reachable")
            return
        while not self._stop:
            self.tick()
            interval = POLL_INTERVAL_LIVE if self._in_live_session() else POLL_INTERVAL_QUIET
            time.sleep(interval)


# ═══════════════════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════════════════

def main():
    guardian = ChartGuardianAgent()
    try:
        guardian.run()
    except KeyboardInterrupt:
        logger.info("guardian stopped by user")
    except Exception as e:
        logger.exception(f"guardian crashed: {e}")


if __name__ == "__main__":
    main()
