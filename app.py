import hmac
import json
import logging
import os
import glob
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from subscriber_fanout import fanout_signal, fanout_exit

# NOVA Algo Discord bridge — best-effort, every call is silent no-op when env
# vars are unset. Never let a Discord post failure break a trade dispatch.
try:
    import nova_algo_discord_bridge as discord_bridge
except Exception as _e:  # noqa: BLE001
    discord_bridge = None

load_dotenv()

app = Flask(__name__)

# ── Webhook auth ──────────────────────────────────────────────────────────────
# Shared secret expected in either the X-Nova-Secret header or a "secret" field
# in the JSON body. If NOVA_WEBHOOK_SECRET is unset, authentication is skipped
# but a loud warning is logged on every request so the operator notices.
NOVA_WEBHOOK_SECRET = os.environ.get("NOVA_WEBHOOK_SECRET", "")

def _webhook_auth_ok(req) -> tuple[bool, str]:
    if not NOVA_WEBHOOK_SECRET:
        return True, "(no secret configured — webhook is open; set NOVA_WEBHOOK_SECRET to lock it down)"
    supplied = req.headers.get("X-Nova-Secret", "")
    if not supplied:
        try:
            body = req.get_json(silent=True) or {}
            supplied = str(body.get("secret", ""))
        except Exception:
            supplied = ""
    if not supplied:
        return False, "missing X-Nova-Secret header (or 'secret' field in body)"
    if not hmac.compare_digest(supplied, NOVA_WEBHOOK_SECRET):
        return False, "invalid webhook secret"
    return True, "ok"

# ── Config ────────────────────────────────────────────────────────────────────
TRADERSPOST_WEBHOOK_URL = os.environ.get("TRADERSPOST_WEBHOOK_URL", "")
MAX_TRADES_PER_DAY      = 5   # 1 Asia + 1 London + 2 NY AM (+1 buffer)

# Per-session caps. NOVA Algo subscriber-facing product is NY-AM-only as of
# 2026-04-27 — Asia and London removed from the routing gate per Sir's call.
# Any Pine alert outside 8:30am-11am ET gets rejected at the session check.
SESSION_TRADE_CAPS = {
    "NY_AM":  2,
}
# Fallback for any future session that isn't in the map
MAX_TRADES_PER_SESSION  = 1
MAX_DAILY_LOSS          = 500.00   # USD
RISK_PER_TRADE          = 500.00   # USD
REWARD_PER_TRADE        = 1000.00  # USD
OBSIDIAN_TRADE_LOG_DIR  = os.environ.get(
    "OBSIDIAN_TRADE_LOG_DIR",
    r"C:\Users\User\nova\nova-brain\01_Trade_Logs",
)

# Grades that are allowed to execute through /webhook and /execute.
# Pine grader only fires A+/A trades anyway, but /execute is callable
# directly from Claude so this is the second gate.
EXECUTABLE_GRADES = {"A+", "A"}

EST = ZoneInfo("America/New_York")

# ── Eval accounts ─────────────────────────────────────────────────────────────
# 2026-04-25: Apex 50K and Lucid 50K both blown — removed from active roster.
# 2026-04-26: Sir picked up a fresh Lucid 50K eval. Adding back at standard
# 6% profit target ($50K → $53K). Apex 100K continues at its prior balance.
EVAL_ACCOUNTS = {
    "apex_100k": {"label": "Apex 100K",  "current": 99532.60, "target": 106000.00},
    "lucid_50k": {"label": "Lucid 50K",  "current": 50000.00, "target": 53000.00},
}

# Default account count a single signal fans out to via TradersPost. Used to
# compute the real daily-loss impact of one losing signal across copy-traded
# accounts. Caller can override per-call via the 'accounts' payload field.
ACTIVE_ACCOUNTS = len(EVAL_ACCOUNTS)

def build_equity_data() -> list[dict]:
    """Calculate progress and dollars remaining for each eval account."""
    result = []
    for account_id, acct in EVAL_ACCOUNTS.items():
        current   = acct["current"]
        target    = acct["target"]
        remaining = round(target - current, 2)
        progress  = round((current / target) * 100, 2) if target else 0.0
        result.append({
            "id":        account_id,
            "label":     acct["label"],
            "current":   current,
            "target":    target,
            "remaining": remaining,
            "progress":  progress,
        })
    return result

# ── Session windows (EST) ─────────────────────────────────────────────────────
# NOVA trades NQ futures, NY-AM only (post-cash-open 9:30am ET → 11am ET).
# 2026-04-27: Sir locked scope to NYSE cash open onward — pre-cash-open 8:30-9:30
# window dropped to skip macro-print whipsaw (CPI/NFP/Powell at 8:30 ET sharp).
# Asia + London sessions removed entirely from the Railway gate.
# Weekend / Saturday remains permanently off.
SESSIONS = {
    "NY_AM":  {"start": (9, 30),  "end": (11, 0)},
}

# NQ-only ticker allowlist. Pine's {{ticker}} can render as any of these
# depending on contract rollover / micro vs mini. Normalized uppercase, with
# optional exchange prefix stripped at the gate.
ALLOWED_TICKERS = {
    "NQ1!", "NQ", "NQU2026", "NQZ2026", "NQH2027", "NQM2026", "NQM2027",  # mini continuous + common quarterly fronts
    "MNQ1!", "MNQ", "MNQU2026", "MNQZ2026", "MNQH2027", "MNQM2026", "MNQM2027",  # micro NQ equivalents
}

# ── In-memory state ───────────────────────────────────────────────────────────
SIGNAL_RING_CAP = 50  # how many recent signals to keep for /signals/recent

state = {
    "date":            None,
    "trades_today":    0,
    "daily_loss":      0.0,
    "session_trades":  {},
    "last_signal":     None,   # {"action", "price", "session", "ticker"}
    "last_signals":    [],     # ring buffer of last SIGNAL_RING_CAP signals (newest first)
    "open_positions":  {},     # keyed by ticker → full signal dict at entry
    "manual_halt":     False,  # /admin/halt sets True; auto-clears on date rollover
}


def _record_signal(sig: dict, now: datetime) -> None:
    """
    Update both `last_signal` and the `last_signals` ring buffer.
    Newest entry is at index 0; buffer is capped at SIGNAL_RING_CAP.
    """
    entry = dict(sig)
    entry["recorded_at"] = now.isoformat()
    state["last_signal"] = sig
    state["last_signals"].insert(0, entry)
    if len(state["last_signals"]) > SIGNAL_RING_CAP:
        state["last_signals"] = state["last_signals"][:SIGNAL_RING_CAP]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("nova_logs.txt"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def reset_daily_state_if_new_day():
    today = date.today()
    if state["date"] != today:
        logger.info(f"New trading day ({today}). Resetting daily state.")
        state["date"]           = today
        state["trades_today"]   = 0
        state["daily_loss"]     = 0.0
        state["session_trades"] = {s: 0 for s in SESSIONS}
        state["manual_halt"]    = False  # auto-clear on day rollover
        # open_positions is intentionally NOT reset on day rollover — a position
        # held across a day boundary (rare but possible) should survive. It's
        # cleared only by /close, /report-result, or an explicit admin call.


def get_current_session(now: datetime) -> str | None:
    """
    Return the active NOVA session name, or None.

    NOVA Algo is NY-AM-only as of 2026-04-27 (post-NYSE cash open).
    The Asia/Sunday/Friday handling below is preserved as defense in depth — if
    SESSIONS ever gets re-extended, weekend/Friday rules still hold.

      • Saturday: market closed all day → None
      • Sunday: no trading (Asia removed from SESSIONS)
      • Mon-Fri 9:30-11am ET: NY_AM
      • All other times: None
    """
    weekday = now.weekday()  # Mon=0 ... Sun=6
    minutes = now.hour * 60 + now.minute

    # Saturday — futures market closed all day
    if weekday == 5:
        return None

    # Sunday — only Asia opens (7 PM-midnight ET); nothing else
    if weekday == 6:
        asia = SESSIONS.get("Asia")
        if not asia:
            return None
        start = asia["start"][0] * 60 + asia["start"][1]
        end   = asia["end"][0]   * 60 + asia["end"][1]
        return "Asia" if start <= minutes < end else None

    # Friday — futures close at 5 PM ET, so no Asia session that evening.
    if weekday == 4:
        for name, window in SESSIONS.items():
            if name == "Asia":
                continue
            start = window["start"][0] * 60 + window["start"][1]
            end   = window["end"][0]   * 60 + window["end"][1]
            if start <= minutes < end:
                return name
        return None

    # Mon-Thu — all sessions valid
    for name, window in SESSIONS.items():
        start = window["start"][0] * 60 + window["start"][1]
        end   = window["end"][0]   * 60 + window["end"][1]
        if start <= minutes < end:
            return name
    return None


def expire_stale_positions(now: datetime) -> list[dict]:
    """
    Defensive state scrub: pop any `open_positions` entry whose session is no
    longer the active session. The Pine strategy closes every position flat at
    session-end; if Railway didn't receive an explicit /close, the tracker
    lingers and blocks the next signal (happened 2026-04-22 03:43 — Asia trade
    ghost blocked a London signal). This helper prevents the re-occurrence by
    inspecting each stored position's session metadata and expiring anything
    that shouldn't still be open.

    Called at the top of evaluate_gates() on every signal. Returns a list of
    expired records for logging/observability.

    Entries created before the session metadata existed (legacy, `session`
    missing on the stored dict) are left alone — /close or a restart clears
    those.
    """
    current_session = get_current_session(now)
    expired: list[dict] = []
    for ticker, pos in list(state["open_positions"].items()):
        opened_session = pos.get("session")
        if not opened_session:
            continue  # legacy entry — leave alone, operator can /close manually
        if opened_session == current_session:
            continue  # still in the session that opened the position — keep

        expired.append({
            "ticker": ticker,
            "opened_session": opened_session,
            "current_session": current_session,
            "opened_at": pos.get("opened_at"),
        })
        state["open_positions"].pop(ticker, None)
        logger.warning(
            f"[expire_stale_positions] popped {ticker} — opened in "
            f"{opened_session}, current session is {current_session or 'None'}"
        )

    return expired


def validate_payload(data: dict) -> tuple[bool, str]:
    # Required for all actions
    if "ticker" not in data:
        return False, "Missing required field: 'ticker'"
    if "action" not in data:
        return False, "Missing required field: 'action'"

    if data["action"] not in ("buy", "sell", "exit"):
        return False, f"Invalid action '{data['action']}' — must be 'buy', 'sell', or 'exit'"

    # Price required only for entries (buy/sell). Exits don't need a target price —
    # TradersPost closes at market on receiving an exit action.
    if data["action"] in ("buy", "sell"):
        if "price" not in data:
            return False, "Missing required field: 'price' (required for buy/sell)"
        try:
            float(data["price"])
        except (TypeError, ValueError):
            return False, f"Invalid price value: '{data['price']}'"

    return True, "OK"


def build_traderspost_payload(data: dict, session: str) -> dict:
    tv_timestamp = data.get("session", "")
    if tv_timestamp:
        logger.info(f"TradingView session timestamp: {tv_timestamp}")

    raw_comment = str(data.get("comment", "")).strip()
    comment     = f"{raw_comment} | {session}" if raw_comment else session

    sentiment_map = {"buy": "bullish", "sell": "bearish"}

    # Pine emits 'qty'; /execute callers sometimes send 'quantity'. Accept both.
    qty_raw = data.get("qty") if data.get("qty") is not None else data.get("quantity", 1)

    payload = {
        "ticker":    data["ticker"].upper().strip(),
        "action":    data["action"],
        "price":     float(data["price"]),
        "quantity":  int(qty_raw or 1),
        "orderType": data.get("orderType", "market"),
        "sentiment": sentiment_map[data["action"]],
        "comment":   comment,
    }

    # Bracket attachments — TradersPost expects nested stopLoss/takeProfit
    # objects. Pine's alert JSON sends flat 'sl'/'tp' fields; translate here so
    # every forwarded order carries its exit orders.
    sl = data.get("sl")
    tp = data.get("tp")
    if sl is not None:
        try:
            payload["stopLoss"]   = {"type": "stop",   "stopPrice":  float(sl)}
        except (TypeError, ValueError):
            logger.warning(f"Invalid sl value ignored: {sl!r}")
    if tp is not None:
        try:
            payload["takeProfit"] = {"limitPrice": float(tp)}
        except (TypeError, ValueError):
            logger.warning(f"Invalid tp value ignored: {tp!r}")

    logger.info(f"TradersPost payload: {json.dumps(payload)}")
    return payload


def build_traderspost_close(ticker: str, comment: str = "") -> dict:
    """TradersPost uses action=exit to close an existing position for a ticker."""
    payload = {
        "ticker":  ticker.upper().strip(),
        "action":  "exit",
        "comment": comment or "NOVA manual close",
    }
    logger.info(f"TradersPost close payload: {json.dumps(payload)}")
    return payload


def forward_to_traderspost(payload: dict) -> tuple[bool, str]:
    if not TRADERSPOST_WEBHOOK_URL:
        return False, "TRADERSPOST_WEBHOOK_URL is not configured"
    try:
        response = requests.post(
            TRADERSPOST_WEBHOOK_URL,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        logger.info(f"TradersPost response: {response.status_code} — {response.text}")
        return True, response.text
    except requests.exceptions.Timeout:
        return False, "TradersPost request timed out"
    except requests.exceptions.ConnectionError as e:
        return False, f"Could not connect to TradersPost: {str(e)}"
    except requests.exceptions.HTTPError as e:
        return False, f"TradersPost returned error: {e.response.status_code} — {e.response.text}"
    except Exception as e:
        return False, f"Unexpected error forwarding to TradersPost: {str(e)}"


# ── Obsidian trade log ────────────────────────────────────────────────────────

SESSION_DISPLAY = {"London": "London", "NY_AM": "NY AM"}
SIDE_MAP        = {"buy": "long", "sell": "short"}
TYPE_MAP        = {"first": "First entry", "cont": "Continuation", "continuation": "Continuation"}


def _trade_log_path(now: datetime, session: str, side: str) -> str:
    """Return the full path for a new trade log file."""
    os.makedirs(OBSIDIAN_TRADE_LOG_DIR, exist_ok=True)
    filename = now.strftime(f"%Y-%m-%d-%H-%M") + f"-{session.lower().replace('_', '')}-{side}.md"
    return os.path.join(OBSIDIAN_TRADE_LOG_DIR, filename)


def log_trade_to_obsidian(data: dict, session: str, now: datetime) -> str | None:
    """
    Write a new trade log markdown file to the Obsidian vault.
    Returns the file path on success, None on failure.
    """
    side        = SIDE_MAP.get(data["action"], data["action"])
    raw_comment = str(data.get("comment", "")).strip().lower()
    trade_type  = TYPE_MAP.get(raw_comment, raw_comment.capitalize() if raw_comment else "First entry")
    entry_price = float(data["price"])
    session_label = SESSION_DISPLAY.get(session, session)

    stop_loss   = data.get("stop_loss",   data.get("sl", "TBD"))
    take_profit = data.get("take_profit", data.get("tp", "TBD"))

    content = f"""# Trade Log — {now.strftime("%Y-%m-%d %H:%M")} EST

**Date:** {now.strftime("%Y-%m-%d")}
**Time:** {now.strftime("%H:%M")} EST
**Session:** {session_label}
**Side:** {side.capitalize()}
**Type:** {trade_type}

---

## Entry

| Field        | Value         |
|--------------|---------------|
| Entry Price  | {entry_price} |
| Stop Loss    | {stop_loss}   |
| Take Profit  | {take_profit} |
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

    path = _trade_log_path(now, session, side)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Obsidian trade log created: {path}")
        return path
    except Exception as e:
        logger.error(f"Failed to write Obsidian trade log: {e}")
        return None


def find_latest_open_trade_log() -> str | None:
    """Return the path of the most recent trade log with Status: Open, or None."""
    pattern = os.path.join(OBSIDIAN_TRADE_LOG_DIR, "*.md")
    files   = sorted(glob.glob(pattern), reverse=True)
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if "| Status      | Open   |" in content:
                return path
        except Exception:
            continue
    return None


def update_trade_log_result(path: str, outcome: str, exit_price: float) -> bool:
    """Update the Result table in an existing trade log file."""
    outcome_map = {"win": "Win", "loss": "Loss", "be": "Breakeven"}
    pnl_map     = {"win": f"+${REWARD_PER_TRADE:.0f}", "loss": f"-${RISK_PER_TRADE:.0f}", "be": "$0"}

    outcome_label = outcome_map.get(outcome, outcome.capitalize())
    pnl_label     = pnl_map.get(outcome, "—")

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        content = content.replace(
            "| Status      | Open   |",
            f"| Status      | Closed |",
        )
        content = content.replace(
            "| Exit Price  | —      |",
            f"| Exit Price  | {exit_price} |",
        )
        content = content.replace(
            "| Outcome     | —      |",
            f"| Outcome     | {outcome_label} |",
        )
        content = content.replace(
            "| P&L         | —      |",
            f"| P&L         | {pnl_label} |",
        )

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"Obsidian trade log updated: {path} — {outcome_label} @ {exit_price}")
        return True
    except Exception as e:
        logger.error(f"Failed to update Obsidian trade log: {e}")
        return False


# ── Gate evaluator (shared by /webhook and /execute) ──────────────────────────

def _normalize_ticker(raw: str) -> str:
    """Strip exchange prefix if present (CME_MINI:NQ1! -> NQ1!) and upper."""
    t = (raw or "").upper().strip()
    if ":" in t:
        t = t.split(":", 1)[1]
    return t


def evaluate_gates(ticker: str, grade: str | None, now: datetime) -> tuple[bool, str, dict]:
    """
    Run every risk gate. Returns (ok, reason_if_blocked, gate_state).
    gate_state is always populated for dry-run reporting.

    Scope (per feedback memory "Trading scope"):
      - Instrument: NQ futures ONLY (ALLOWED_TICKERS allowlist)
      - Sessions:   London (02:00-05:00 EST) + NY AM (08:30-11:00 EST) ONLY
      - Weekends:   all futures trading rejected
    """
    # Defense #2 — scrub any open_positions entries whose session has ended
    # before we check the open_position gate. Prevents ghost state from
    # blocking legitimate signals in a fresh session (observed 2026-04-22).
    stale = expire_stale_positions(now)
    if stale:
        logger.info(f"[evaluate_gates] expired {len(stale)} stale position(s): {stale}")

    norm       = _normalize_ticker(ticker)
    is_allowed = norm in ALLOWED_TICKERS
    is_weekend = now.weekday() >= 5
    session    = get_current_session(now)

    session_count = state["session_trades"].get(session, 0) if session else 0

    gate_state = {
        "now_est":         now.strftime("%Y-%m-%d %H:%M:%S"),
        "session":         session,
        "is_weekend":      is_weekend,
        "ticker_norm":     norm,
        "ticker_allowed":  is_allowed,
        "session_trades":  session_count,
        "trades_today":    state["trades_today"],
        "daily_loss":      state["daily_loss"],
        "open_position":   norm in state["open_positions"],
        "grade":           grade,
    }

    # Hard instrument allowlist — first gate so misconfigured alerts never
    # even reach the session check
    if not is_allowed:
        return False, f"Rejected — NOVA trades NQ futures only (got '{ticker}')", gate_state
    if is_weekend:
        return False, f"Rejected — futures market closed on weekend ({norm})", gate_state
    if session is None:
        return False, f"Rejected — outside NY AM session 9:30-11:00 ET ({now.strftime('%H:%M %Z')})", gate_state
    session_cap = SESSION_TRADE_CAPS.get(session, MAX_TRADES_PER_SESSION)
    gate_state["session_cap"] = session_cap
    if session_count >= session_cap:
        return False, f"Rejected — max {session_cap} trade(s) already in {session}", gate_state
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, f"Rejected — daily trade limit of {MAX_TRADES_PER_DAY} reached", gate_state
    if state["daily_loss"] >= MAX_DAILY_LOSS:
        return False, f"Rejected — daily loss limit of ${MAX_DAILY_LOSS:.2f} reached", gate_state
    if state.get("manual_halt"):
        return False, "Rejected — pipeline manually halted by staff", gate_state
    if norm in state["open_positions"]:
        return False, f"Rejected — position already open for {norm}", gate_state

    return True, "OK", gate_state


def _handle_exit_signal(data: dict, now):
    """Handle an exit signal — bypasses all gates. Forwards exit to Sir's
    primary TP + fanouts exit to every approved subscriber's TP. Clears the
    Railway open_positions tracker for this ticker.

    Triggered by Pine alert(action="exit") when:
      - active SL is touched (covers original SL, BE move, Trail move)
      - session close at 11:00 ET with position still open
    """
    ticker = str(data.get("ticker", "")).upper().strip()
    if not ticker:
        return jsonify({"status": "error", "message": "ticker required for exit"}), 400

    comment = str(data.get("comment", "NOVA exit"))
    logger.info(f"/webhook EXIT — {ticker} | {comment}")

    # Forward to Sir's primary TP (Apex/Lucid stack)
    tp_payload = build_traderspost_close(ticker, comment)
    primary_ok, primary_resp = forward_to_traderspost(tp_payload)
    if not primary_ok:
        logger.error(f"/webhook exit — primary TP forward failed: {primary_resp}")

    # Fanout exit to every approved subscriber's TP
    try:
        fanout_result = fanout_exit(data)
        logger.info(
            f"[fanout-exit] {fanout_result.get('ok',0)}/{fanout_result.get('fanned_to',0)} "
            f"subscribers received exit ({fanout_result.get('fail',0)} failed)"
        )
    except Exception as fanout_err:  # noqa: BLE001
        logger.warning(f"[fanout-exit] unhandled error: {fanout_err}")
        fanout_result = {"fanned_to": 0, "ok": 0, "fail": 0, "details": []}

    # Capture entry context BEFORE popping state — needed for #trade-journal post.
    open_position = state["open_positions"].get(ticker)

    # Clear Railway state — kills the ghost position tracker
    state["open_positions"].pop(ticker, None)

    # Auto post-mortem to #trade-journal (best-effort; never fails the exit path).
    if discord_bridge:
        try:
            entry_price = float((open_position or {}).get("price", 0) or 0)
            exit_price  = float(data.get("price", 0) or 0)
            opened_at   = (open_position or {}).get("opened_at")
            side        = ((open_position or {}).get("action") or "").lower()
            is_long     = side in ("buy", "long")

            # Parse exit reason from Pine comment, e.g. "NY_AM_ORB_TrailExit"
            reason = "Close"
            if "TrailExit" in comment: reason = "TrailExit"
            elif "BEExit" in comment:  reason = "BEExit"
            elif "SLExit" in comment:  reason = "SL"
            elif "TP"     in comment:  reason = "TP"
            elif "Session" in comment: reason = "SessionFlat"

            # NQ contract = $20/pt for the mini, $2/pt for the micro. Default to mini.
            point_value = 20.0
            if entry_price and exit_price:
                pts = (exit_price - entry_price) if is_long else (entry_price - exit_price)
                usd_pnl = pts * point_value
                # SL is $500 by canonical strategy → R = pnl / 500
                r_multiple = usd_pnl / 500.0
            else:
                usd_pnl = 0.0
                r_multiple = 0.0

            hold_min = None
            if opened_at:
                try:
                    opened_dt = datetime.fromisoformat(opened_at)
                    delta = now - opened_dt
                    hold_min = int(delta.total_seconds() // 60)
                except Exception:
                    hold_min = None

            discord_bridge.post_trade_journal({
                "ticker":      ticker,
                "side":        side or ("buy" if is_long else "sell"),
                "entry":       entry_price,
                "exit":        exit_price,
                "exit_reason": reason,
                "r_multiple":  r_multiple,
                "usd_pnl":     usd_pnl,
                "hold_min":    hold_min,
                "opened_at":   opened_at,
                "closed_at":   now.isoformat(),
            })
        except Exception as journal_err:
            logger.warning(f"[trade-journal] post failed: {journal_err}")

    return jsonify({
        "status":     "exit_executed",
        "ticker":     ticker,
        "comment":    comment,
        "primary_ok": primary_ok,
        "fanout":     {
            "fanned_to": fanout_result.get("fanned_to", 0),
            "ok":        fanout_result.get("ok", 0),
            "fail":      fanout_result.get("fail", 0),
        },
    }), 200


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    now = datetime.now(tz=EST)
    logger.info(f"Incoming webhook — {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # 0. Auth — shared secret (header or body)
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        logger.warning(f"Webhook auth failed: {reason}")
        return jsonify({"status": "unauthorized", "message": reason}), 401
    if not NOVA_WEBHOOK_SECRET:
        logger.warning("⚠ NOVA_WEBHOOK_SECRET is not set — webhook is open to anyone who knows the URL")

    # 1. Parse JSON
    try:
        data = request.get_json(force=True, silent=False)
        if data is None:
            raise ValueError("Empty or non-JSON body")
    except Exception as e:
        logger.warning(f"Failed to parse request body: {e}")
        return jsonify({"status": "error", "message": "Invalid JSON payload"}), 400

    logger.info(f"Raw payload received: {json.dumps(data)}")

    # Grade-aware compact log line for forward-test analysis. Pine v1.4.2+
    # emits `grade` (A+/A/A-/B+/B) and `grade_score` (0-100) on every entry.
    _g = data.get("grade")
    _gs = data.get("grade_score") if data.get("grade_score") is not None else data.get("score")
    if _g is not None or _gs is not None:
        logger.info(
            f"[grader] action={data.get('action')} ticker={data.get('ticker')} "
            f"grade={_g} score={_gs} comment={data.get('comment')}"
        )

    # 2. Validate required fields
    valid, validation_message = validate_payload(data)
    if not valid:
        logger.warning(f"Payload validation failed: {validation_message}")
        return jsonify({"status": "error", "message": validation_message}), 400

    # 2.5. Exit action: short-circuit before gates. Exits are ALWAYS allowed —
    # outside session, past daily-loss cap, weekend, etc. — because flattening
    # is the safety valve.
    if data.get("action") == "exit":
        return _handle_exit_signal(data, now)

    # 3. Reset daily state if new day
    reset_daily_state_if_new_day()

    # 4. Hand off to the Trading Commander — it runs the whole chain:
    #    enrich → gate → dispatch (TradersPost w/ retry → ManualEscalation)
    #    → observability (Discord mirror + ledger)
    commander = _get_commander()
    result    = commander.handle(data)

    if result.status == "rejected":
        # Hard-limit rejections (DD hit, daily cap, session cap) are interesting
        # to subscribers — surface them in #halt-events. Grade/auth rejections
        # stay silent so the channel doesn't fill with noise.
        if discord_bridge:
            try:
                msg = (result.message or "").lower()
                if any(w in msg for w in ("loss limit", "daily limit", "max ", "trade limit", "already open")):
                    discord_bridge.post_gate_rejection(
                        ticker=(result.enriched.ticker if result.enriched else (data.get("ticker") or "?")),
                        reason=result.message,
                        gate_state=result.gates or {},
                    )
            except Exception as _be:  # noqa: BLE001
                logger.warning(f"[discord-bridge] gate rejection notify failed: {_be}")
        return jsonify({
            "status":  "rejected",
            "message": result.message,
            "gates":   result.gates,
            "signal_id": result.signal_id,
        }), 200

    if result.status == "error":
        if discord_bridge and result.enriched and result.dispatch:
            try:
                discord_bridge.post_signal_failed(result.enriched, result.dispatch)
            except Exception as _be:  # noqa: BLE001
                logger.warning(f"[discord-bridge] signal_failed notify failed: {_be}")
        return jsonify({
            "status":  "error",
            "message": result.message,
            "signal_id": result.signal_id,
        }), 502

    # status in ("executed", "escalated") — treat both as successful intake:
    # "executed" means TP accepted, "escalated" means we queued + pinged Sir.
    # In both cases, update state as if the trade is live (a queued trade
    # that Sir taps within 30 min is still a real trade, and we want the
    # session/day limits to reflect that). If Sir lets it expire, we can
    # reconcile via /admin/unqueue later.
    session = result.gates.get("session") or "unknown"
    state["trades_today"] += 1
    state["session_trades"][session] = result.gates.get("session_trades", 0) + 1
    enriched = result.enriched
    _record_signal({
        "action":  enriched.action,
        "price":   enriched.price,
        "session": session,
        "ticker":  enriched.ticker,
        "sl":      enriched.sl,
        "tp":      enriched.tp,
        "be":      enriched.be,
        "grade":   enriched.grade,
        "score":   enriched.score,
        "sweep":   enriched.sweep,
        "source":  "webhook",
        "signal_id": enriched.signal_id,
        "status":  result.status,
    }, now)
    state["open_positions"][enriched.ticker] = dict(state["last_signal"], opened_at=now.isoformat())

    # Fan the same raw payload out to every NOVA Algo subscriber's TradersPost
    # webhook. Runs after our own routing — never blocks the founder's fills.
    # Subscribers are pulled from novaalgo.org (Clerk users with submitted URLs).
    try:
        fanout_result = fanout_signal(data)
        logger.info(
            f"[fanout] {fanout_result['ok']}/{fanout_result['fanned_to']} "
            f"subscribers received the signal "
            f"({fanout_result['fail']} failed)"
        )
    except Exception as fanout_err:  # noqa: BLE001
        logger.warning(f"[fanout] unhandled fanout error: {fanout_err}")
        fanout_result = {"fanned_to": 0, "ok": 0, "fail": 0, "details": []}

    # Mirror this signal to the NOVA Algo Discord — #live-signals + #fanout-failures
    # if any subscribers failed. Best-effort; never blocks the response.
    if discord_bridge and result.enriched and result.dispatch:
        try:
            discord_bridge.post_signal_executed(
                result.enriched, result.dispatch, fanout_summary=fanout_result,
            )
            failures = [d for d in (fanout_result.get("details") or []) if not d.get("ok")]
            if failures:
                discord_bridge.post_fanout_failures(failures, total=fanout_result.get("fanned_to", 0))
        except Exception as _be:  # noqa: BLE001
            logger.warning(f"[discord-bridge] live-signals mirror failed: {_be}")

    logger.info(
        f"Signal {result.status} — session: {session} | "
        f"session trades: {state['session_trades'][session]}/{MAX_TRADES_PER_SESSION} | "
        f"day trades: {state['trades_today']}/{MAX_TRADES_PER_DAY} | "
        f"daily loss: ${state['daily_loss']:.2f}/${MAX_DAILY_LOSS:.2f} | "
        f"chain: {[a.message for a in (result.dispatch.attempts if result.dispatch else [])]}"
    )

    # Trade is now in /agents/ledger as signal_executed. The desktop-side
    # TradeMemorializer (nova_assistant.py) polls the ledger and writes the
    # trade into the Neural Brain as a memory. Railway's filesystem isn't
    # Sir's Obsidian vault so we don't bother with local file logging here.

    response = {
        "status":         "ok" if result.status == "executed" else result.status,
        "message":        result.message,
        "signal_id":      result.signal_id,
        "session":        session,
        "trades_today":   state["trades_today"],
        "session_trades": state["session_trades"][session],
        "daily_loss":     state["daily_loss"],
        "dispatch": {
            "chosen":   result.dispatch.chosen if result.dispatch else None,
            "attempts": [
                {"venue": a.venue, "success": a.success, "message": a.message}
                for a in (result.dispatch.attempts if result.dispatch else [])
            ],
        },
        "fanout": {
            "fanned_to": fanout_result["fanned_to"],
            "ok":        fanout_result["ok"],
            "fail":      fanout_result["fail"],
        },
    }

    # Mirror to #signal-audit (staff). Best-effort; never fails the webhook.
    if discord_bridge:
        try:
            discord_bridge.post_signal_audit(
                payload=data,
                gates={
                    "session": session,
                    "trades_today": state["trades_today"],
                    "daily_loss": state["daily_loss"],
                    "result": result.status,
                },
                dispatch_result={
                    "primary_ok": result.dispatch.chosen if result.dispatch else None,
                    "fanned_to":  fanout_result["fanned_to"],
                    "ok":         fanout_result["ok"],
                    "fail":       fanout_result["fail"],
                },
            )
        except Exception as audit_err:
            logger.warning(f"[signal-audit] post failed: {audit_err}")

    return jsonify(response), 200


# ── Manual execution endpoint (Claude / MCP / voice) ─────────────────────────

@app.route("/execute", methods=["POST"])
def execute_manual():
    """
    Manual trade execution from Claude/MCP/voice.

    Payload:
      { "ticker": "NQ1!", "action": "buy"|"sell", "price": 21500.00,
        "sl": 21475.00, "tp": 21550.00, "be": 21525.00,
        "grade": "A+"|"A"|"B"|"C", "sweep": "PDL", "comment": "FVG_LONG_MANUAL",
        "dry_run": true,    // defaults TRUE — must explicitly set false to fire live
        "force":   false }   // set true to bypass grade filter (still subject to session/DD gates)

    In dry_run mode: returns the payload that WOULD be forwarded + gate state,
    makes NO TradersPost call, mutates NO state.
    """
    now = datetime.now(tz=EST)
    logger.info(f"/execute called — {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    try:
        data = request.get_json(force=True, silent=False)
        if data is None:
            raise ValueError("Empty body")
    except Exception as e:
        return jsonify({"status": "error", "message": f"Invalid JSON: {e}"}), 400

    valid, validation_message = validate_payload(data)
    if not valid:
        return jsonify({"status": "error", "message": validation_message}), 400

    # dry_run defaults TRUE — this is deliberate; must explicitly opt in to live
    dry_run = bool(data.get("dry_run", True))
    force   = bool(data.get("force", False))
    grade   = data.get("grade")
    ticker  = data.get("ticker", "").upper().strip()

    reset_daily_state_if_new_day()

    # Grade gate
    grade_ok = (grade in EXECUTABLE_GRADES) or force
    if not grade_ok:
        return jsonify({
            "status":  "rejected",
            "message": f"Grade '{grade}' not in executable set {sorted(EXECUTABLE_GRADES)}. Pass force=true to override.",
            "grade":   grade,
        }), 200

    # Risk gates
    gates_ok, reason, gate_state = evaluate_gates(ticker, grade, now)
    if not gates_ok:
        return jsonify({
            "status":  "rejected",
            "message": reason,
            "gates":   gate_state,
        }), 200

    session = gate_state["session"]
    tp_payload = build_traderspost_payload(data, session)

    # ── Dry run — return intent, don't fire ──
    if dry_run:
        logger.info(f"/execute DRY RUN — would forward {json.dumps(tp_payload)}")
        return jsonify({
            "status":        "dry_run",
            "message":       "Dry run — no order sent. Pass dry_run=false to fire live.",
            "would_forward": tp_payload,
            "gates":         gate_state,
            "grade":         grade,
            "force":         force,
        }), 200

    # ── Live execution ──
    success, tp_response = forward_to_traderspost(tp_payload)
    if not success:
        logger.error(f"/execute — TradersPost forward failed: {tp_response}")
        return jsonify({"status": "error", "message": tp_response}), 502

    # Mutate state
    state["trades_today"] += 1
    state["session_trades"][session] = gate_state["session_trades"] + 1
    _record_signal({
        "action":  data["action"],
        "price":   float(data["price"]),
        "session": session,
        "ticker":  ticker,
        "sl":      float(data["sl"])    if data.get("sl")    else None,
        "tp":      float(data["tp"])    if data.get("tp")    else None,
        "be":      float(data["be"])    if data.get("be")    else None,
        "grade":   grade,
        "score":   int(data["score"])   if data.get("score") else None,
        "sweep":   data.get("sweep"),
        "source":  "execute",
    }, now)
    state["open_positions"][ticker] = dict(state["last_signal"], opened_at=now.isoformat())

    log_path = log_trade_to_obsidian(data, session, now)

    logger.info(
        f"/execute LIVE — {ticker} {data['action']} @ {data['price']} | "
        f"grade {grade} | session {session} | "
        f"day {state['trades_today']}/{MAX_TRADES_PER_DAY}"
    )

    return jsonify({
        "status":         "ok",
        "dry_run":        False,
        "forwarded":      True,
        "session":        session,
        "ticker":         ticker,
        "action":         data["action"],
        "grade":          grade,
        "trades_today":   state["trades_today"],
        "session_trades": state["session_trades"][session],
        "trade_log":      log_path or "failed to write",
    }), 200


# ── Close position endpoint ───────────────────────────────────────────────────

@app.route("/close", methods=["POST"])
def close_position():
    """
    Close an open position for a given ticker.

    Payload:
      { "ticker": "NQ1!",
        "dry_run": true,           // defaults TRUE
        "comment": "manual close",
        "outcome": "win"|"loss"|"be",  // optional — if provided, logs result + updates daily_loss
        "exit_price": 21550.00 }       // optional — paired with outcome

    Always permitted regardless of session/grade — close is the safety valve.
    """
    now = datetime.now(tz=EST)

    try:
        data = request.get_json(force=True, silent=False)
        if data is None:
            raise ValueError("Empty body")
    except Exception as e:
        return jsonify({"status": "error", "message": f"Invalid JSON: {e}"}), 400

    ticker = str(data.get("ticker", "")).upper().strip()
    if not ticker:
        return jsonify({"status": "error", "message": "ticker is required"}), 400

    dry_run  = bool(data.get("dry_run", True))
    comment  = str(data.get("comment", "NOVA manual close"))
    outcome  = str(data.get("outcome", "")).strip().lower()
    exit_px  = data.get("exit_price")

    try:
        accounts = int(data.get("accounts", ACTIVE_ACCOUNTS))
        if accounts < 1:
            raise ValueError("accounts must be >= 1")
    except (TypeError, ValueError) as e:
        return jsonify({"status": "error", "message": f"Invalid accounts: {e}"}), 400

    position = state["open_positions"].get(ticker)
    tp_payload = build_traderspost_close(ticker, comment)

    if dry_run:
        return jsonify({
            "status":        "dry_run",
            "message":       "Dry run — no close sent. Pass dry_run=false to fire live.",
            "would_forward": tp_payload,
            "open_position": position,
        }), 200

    success, tp_response = forward_to_traderspost(tp_payload)
    if not success:
        logger.error(f"/close — TradersPost forward failed: {tp_response}")
        return jsonify({"status": "error", "message": tp_response}), 502

    # Optional outcome logging
    outcome_info = None
    if outcome in ("win", "loss", "be"):
        try:
            exit_px_f = float(exit_px) if exit_px is not None else None
        except (TypeError, ValueError):
            exit_px_f = None

        log_path = find_latest_open_trade_log()
        if log_path and exit_px_f is not None:
            update_trade_log_result(log_path, outcome, exit_px_f)

        actual_loss = 0.0
        if outcome == "loss":
            reset_daily_state_if_new_day()
            actual_loss = RISK_PER_TRADE * accounts
            state["daily_loss"] += actual_loss
            logger.info(
                f"Daily loss updated via /close: ${state['daily_loss']:.2f}/${MAX_DAILY_LOSS:.2f} "
                f"(+${actual_loss:.2f} from {accounts} account(s))"
            )

        outcome_info = {
            "outcome":     outcome,
            "exit_price":  exit_px_f,
            "accounts":    accounts,
            "actual_loss": actual_loss,
        }

    # Clear the tracked position
    state["open_positions"].pop(ticker, None)

    logger.info(f"/close LIVE — {ticker} closed | outcome={outcome or '—'}")

    return jsonify({
        "status":        "ok",
        "dry_run":       False,
        "forwarded":     True,
        "ticker":        ticker,
        "action":        "exit",
        "outcome":       outcome_info,
        "daily_loss":    state["daily_loss"],
        "loss_limit":    MAX_DAILY_LOSS,
    }), 200


# ── Report result endpoint ────────────────────────────────────────────────────

@app.route("/report-result", methods=["POST"])
def report_result():
    """
    Payload: { "outcome": "win" | "loss" | "be", "exit_price": 21550.00, "ticker": "NQ1!" }
    Finds the most recent open trade log and updates it with the result.
    If ticker is passed, also clears the open_positions tracker for that ticker.
    """
    try:
        data = request.get_json(force=True, silent=False)
        if data is None:
            raise ValueError("Empty body")
    except Exception as e:
        return jsonify({"status": "error", "message": f"Invalid JSON: {e}"}), 400

    outcome    = str(data.get("outcome", "")).strip().lower()
    exit_price = data.get("exit_price")
    ticker     = str(data.get("ticker", "")).upper().strip()

    try:
        accounts = int(data.get("accounts", ACTIVE_ACCOUNTS))
        if accounts < 1:
            raise ValueError("accounts must be >= 1")
    except (TypeError, ValueError) as e:
        return jsonify({"status": "error", "message": f"Invalid accounts: {e}"}), 400

    if outcome not in ("win", "loss", "be"):
        return jsonify({"status": "error", "message": "outcome must be 'win', 'loss', or 'be'"}), 400

    try:
        exit_price = float(exit_price)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "exit_price must be a number"}), 400

    path = find_latest_open_trade_log()
    if not path:
        return jsonify({"status": "error", "message": "No open trade log found"}), 404

    success = update_trade_log_result(path, outcome, exit_price)
    if not success:
        return jsonify({"status": "error", "message": "Failed to update trade log"}), 500

    # Update daily loss state if trade was a loss — multiplied by the number
    # of copy-traded accounts the signal filled on (default: all 3).
    actual_loss = 0.0
    if outcome == "loss":
        reset_daily_state_if_new_day()
        actual_loss = RISK_PER_TRADE * accounts
        state["daily_loss"] += actual_loss
        logger.info(
            f"Daily loss updated: ${state['daily_loss']:.2f}/${MAX_DAILY_LOSS:.2f} "
            f"(+${actual_loss:.2f} from {accounts} account(s))"
        )

    # Clear tracked position if ticker supplied
    if ticker:
        state["open_positions"].pop(ticker, None)

    return jsonify({
        "status":        "ok",
        "message":       "Trade log updated",
        "outcome":       outcome,
        "exit_price":    exit_price,
        "accounts":      accounts,
        "actual_loss":   actual_loss,
        "daily_loss":    state["daily_loss"],
        "loss_limit":    MAX_DAILY_LOSS,
        "loss_remaining": max(0.0, MAX_DAILY_LOSS - state["daily_loss"]),
        "log_file":      os.path.basename(path),
    }), 200


# ── Loss reporting endpoint ───────────────────────────────────────────────────

@app.route("/report-loss", methods=["POST"])
def report_loss():
    """Payload: { "loss": 250.00 }"""
    try:
        data = request.get_json(force=True, silent=False)
        loss = float(data["loss"])
        if loss < 0:
            raise ValueError("Loss value must be positive")
    except (TypeError, KeyError, ValueError) as e:
        return jsonify({"status": "error", "message": f"Invalid payload: {e}"}), 400

    reset_daily_state_if_new_day()
    state["daily_loss"] += loss
    logger.info(f"Loss reported: ${loss:.2f} | Total daily loss: ${state['daily_loss']:.2f}")

    return jsonify({
        "status":     "ok",
        "daily_loss":  state["daily_loss"],
        "limit":       MAX_DAILY_LOSS,
        "remaining":   max(0.0, MAX_DAILY_LOSS - state["daily_loss"]),
    }), 200


# ── Positions endpoint ────────────────────────────────────────────────────────

@app.route("/positions", methods=["GET"])
def positions():
    """Return current open positions tracked in server state."""
    return jsonify({
        "status":          "ok",
        "open_positions":  state["open_positions"],
        "count":           len(state["open_positions"]),
    }), 200


# ── Equity endpoint ───────────────────────────────────────────────────────────

@app.route("/equity", methods=["GET"])
def equity():
    """Return current equity, target, progress %, and dollars remaining per account."""
    return jsonify({
        "status":   "ok",
        "accounts": build_equity_data(),
    }), 200


@app.route("/equity/<account_id>", methods=["PATCH"])
def update_equity(account_id):
    """
    Update current equity for one account.
    Payload: { "current": 49100.00 }
    """
    if account_id not in EVAL_ACCOUNTS:
        return jsonify({"status": "error", "message": f"Unknown account '{account_id}'"}), 404

    try:
        data    = request.get_json(force=True, silent=False)
        current = float(data["current"])
        if current < 0:
            raise ValueError("current must be non-negative")
    except (TypeError, KeyError, ValueError) as e:
        return jsonify({"status": "error", "message": f"Invalid payload: {e}"}), 400

    EVAL_ACCOUNTS[account_id]["current"] = current
    logger.info(f"Equity updated — {account_id}: ${current:.2f}")

    accounts = build_equity_data()
    updated  = next(a for a in accounts if a["id"] == account_id)
    return jsonify({"status": "ok", "account": updated}), 200


# ── Status endpoint ───────────────────────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    reset_daily_state_if_new_day()
    now     = datetime.now(tz=EST)
    session = get_current_session(now)
    return jsonify({
        "time_est":        now.strftime("%Y-%m-%d %H:%M:%S"),
        "active_session":  session or "None",
        "trades_today":    state["trades_today"],
        "session_trades":  state["session_trades"],
        "daily_loss":      state["daily_loss"],
        "loss_limit":      MAX_DAILY_LOSS,
        "loss_remaining":  max(0.0, MAX_DAILY_LOSS - state["daily_loss"]),
        "last_signal":     state.get("last_signal"),
        "open_positions":  state["open_positions"],
        "equity":          build_equity_data(),
    }), 200


# ── Discord bridge endpoints (admin / observability) ─────────────────────────

@app.route("/discord/test", methods=["GET", "POST"])
def discord_test():
    """Fire one test embed to each configured NOVA Algo Discord channel.

    Auth: requires NOVA_WEBHOOK_SECRET as `?secret=...` or X-Nova-Secret header.
    Public if NOVA_WEBHOOK_SECRET is unset (matches /webhook auth model).
    """
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    results = discord_bridge.smoke_test()
    return jsonify({"status": "ok", "results": results}), 200


@app.route("/discord/equity/post", methods=["POST"])
def discord_equity_post():
    """Push current equity snapshot to #equity-curve. Used by daily cron.

    Body may override `accounts` (preferred — Vercel cron computes from Clerk
    fills) and `day_pnl_total`. Falls back to build_equity_data() (which reads
    EVAL_ACCOUNTS hardcoded dict, prone to drift) when accounts is omitted.
    """
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}
    day_pnl = body.get("day_pnl_total")
    try:
        day_pnl = float(day_pnl) if day_pnl is not None else None
    except (TypeError, ValueError):
        day_pnl = None
    # Prefer caller-supplied accounts (Clerk-derived, accurate) over the
    # hardcoded EVAL_ACCOUNTS dict.
    accounts = body.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        accounts = build_equity_data()
    ok = discord_bridge.post_equity_snapshot(accounts, day_pnl_total=day_pnl)
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


@app.route("/discord/eod/post", methods=["POST"])
def discord_eod_post():
    """Push rich EOD recap to #eod-recap. Cron weekdays 11:05 ET."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}

    today_prefix = datetime.now(EST).strftime("%Y-%m-%d")
    today_signals = [
        s for s in state["last_signals"]
        if (s.get("recorded_at") or "")[:10] == today_prefix
    ]
    last_signal = today_signals[0] if today_signals else None

    cohort_pnl_raw = body.get("cohort_pnl")
    cohort_pnl = float(cohort_pnl_raw) if isinstance(cohort_pnl_raw, (int, float)) else None
    cohort_traders_raw = body.get("cohort_traders")
    cohort_traders = int(cohort_traders_raw) if isinstance(cohort_traders_raw, (int, float)) else None

    ok = discord_bridge.post_eod_recap(
        trades_today=int(body.get("trades_today", state["trades_today"])),
        wins=int(body.get("wins", 0)),
        losses=int(body.get("losses", 0)),
        breakeven=int(body.get("breakeven", 0)),
        day_pnl=float(body.get("day_pnl", -state["daily_loss"])),
        notes=body.get("notes"),
        last_signal=body.get("last_signal", last_signal),
        equity=body.get("equity", build_equity_data()),
        open_positions=body.get("open_positions", state["open_positions"]),
        pipeline_note=body.get("pipeline_note"),
        cohort_pnl=cohort_pnl,
        cohort_traders=cohort_traders,
    )
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


@app.route("/discord/morning/post", methods=["POST"])
def discord_morning_post():
    """Push morning brief to #morning-brief. Cron at 08:00 ET."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}
    ok = discord_bridge.post_morning_brief(
        bias=body.get("bias"),
        levels=body.get("levels") or {},
        conditions=body.get("conditions"),
        notes=body.get("notes"),
    )
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


def _compute_nq_levels() -> dict:
    """Pull NQ=F daily candles via yfinance and derive PDH/PDL/weekly-open/etc."""
    import yfinance as yf
    try:
        hist = yf.Ticker("NQ=F").history(period="14d", interval="1d")
        if hist is None or hist.empty:
            return {}
        rows = hist.tail(10)
        last = rows.iloc[-1]
        prior = rows.iloc[-2] if len(rows) >= 2 else None
        last_5 = rows.tail(5)

        # Weekly open: most recent Monday's open (or Sunday 6pm bar — yfinance daily dates Monday)
        weekly_open = None
        for ts, row in rows.iterrows():
            if ts.weekday() == 0:  # Monday
                weekly_open = float(row["Open"])
        prior_week = rows.iloc[-7:-2] if len(rows) >= 7 else None

        out = {
            "as_of": datetime.now(EST).strftime("%a %b %d, %Y"),
            "symbol": "NQ1!",
            "current": float(last["Close"]),
            "pdh": float(prior["High"]) if prior is not None else None,
            "pdl": float(prior["Low"]) if prior is not None else None,
            "weekly_open": weekly_open,
            "session_h_5d": float(last_5["High"].max()) if not last_5.empty else None,
            "session_l_5d": float(last_5["Low"].min()) if not last_5.empty else None,
        }
        if prior_week is not None and not prior_week.empty:
            out["prior_week_h"] = float(prior_week["High"].max())
            out["prior_week_l"] = float(prior_week["Low"].min())
        # Estimated OR width from rolling 20d (high - low avg, scaled to a typical 30m span)
        try:
            full = yf.Ticker("NQ=F").history(period="40d", interval="1d")
            daily_range = (full["High"] - full["Low"]).tail(20).mean()
            out["expected_or_width"] = float(daily_range) * 0.18  # ~18% of full daily range
        except Exception:
            out["expected_or_width"] = None
        return out
    except Exception as e:
        logger.warning(f"[key-levels] yfinance fetch failed: {e}")
        return {}


def _fetch_macro_events_today() -> list[dict]:
    """Pull today's high-impact USD macro events from ForexFactory's free JSON."""
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=6.0,
            headers={"User-Agent": "NOVA-Algo-Bot/1.0"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        today_iso = datetime.now(EST).strftime("%Y-%m-%d")
        events = []
        for ev in data:
            ts = ev.get("date") or ""
            if not ts.startswith(today_iso):
                continue
            currency = (ev.get("country") or ev.get("currency") or "").upper()
            if currency not in ("USD", ""):
                continue
            impact = (ev.get("impact") or "").lower()
            time_part = ts[11:16] if len(ts) >= 16 else "—"
            events.append({
                "time": f"{time_part} ET",
                "title": ev.get("title", "Event"),
                "currency": currency or "USD",
                "impact": impact,
                "forecast": ev.get("forecast") or None,
                "previous": ev.get("previous") or None,
            })
        # high impact first, then by time
        events.sort(key=lambda e: (0 if e["impact"] == "high" else (1 if e["impact"] == "medium" else 2), e["time"]))
        return events
    except Exception as e:
        logger.warning(f"[news-feed] macro fetch failed: {e}")
        return []


@app.route("/discord/key-levels/post", methods=["POST"])
def discord_key_levels_post():
    """Push #key-levels daily card. Cron 7:30 ET weekdays."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    levels = _compute_nq_levels()
    if not levels:
        return jsonify({"status": "skipped", "reason": "no-data"}), 200
    ok = discord_bridge.post_key_levels(levels)
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok), "levels": levels}), 200


@app.route("/discord/news-feed/post", methods=["POST"])
def discord_news_feed_post():
    """Push #news-feed daily macro card. Cron 7:00 ET weekdays."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    events = _fetch_macro_events_today()
    ok = discord_bridge.post_news_feed(events)
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok), "count": len(events)}), 200


@app.route("/discord/pre-market/post", methods=["POST"])
def discord_pre_market_post():
    """Push #pre-market 9:00 ET snapshot."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    levels = _compute_nq_levels()
    events = _fetch_macro_events_today()
    next_event = next(
        (e for e in events if e["impact"] == "high"),
        events[0] if events else None,
    )
    snapshot = {
        "as_of": datetime.now(EST).strftime("%a %b %d, %I:%M %p ET").replace(" 0", " "),
        "current": levels.get("current"),
        "pdh": levels.get("pdh"),
        "pdl": levels.get("pdl"),
        "weekly_open": levels.get("weekly_open"),
        "news_count": len(events),
        "next_event": next_event,
        "expected_or_width": levels.get("expected_or_width"),
    }
    ok = discord_bridge.post_pre_market(snapshot)
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


@app.route("/discord/trade-journal/post", methods=["POST"])
def discord_trade_journal_post():
    """Receive a close event payload and post to #trade-journal."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}
    if not body.get("ticker"):
        return jsonify({"status": "bad-request", "message": "ticker required"}), 400
    ok = discord_bridge.post_trade_journal(body)
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


@app.route("/discord/status/heartbeat", methods=["POST"])
def discord_status_heartbeat():
    """Hourly Railway heartbeat → #status."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    now = datetime.now(tz=EST)
    session = get_current_session(now) or "None"
    sub_count = 0
    try:
        from subscriber_fanout import _fetch_subscribers
        sub_count = len(_fetch_subscribers() or [])
    except Exception:
        sub_count = 0
    snapshot = {
        "active_session":  session,
        "trades_today":    state["trades_today"],
        "daily_loss":      state["daily_loss"],
        "loss_limit":      MAX_DAILY_LOSS,
        "open_positions":  state["open_positions"],
        "approved_subscribers": sub_count,
    }
    ok = discord_bridge.post_status_heartbeat(snapshot)
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


@app.route("/discord/stats-dashboard/post", methods=["POST"])
def discord_stats_dashboard_post():
    """Nightly merged stats → #stats-dashboard. Body may pass merged stats; if not, we fetch."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}
    stats = body.get("merged") or body.get("stats")
    if not stats:
        try:
            r = requests.get("https://novaalgo.org/api/stats/live", timeout=6)
            data = r.json() if r.status_code == 200 else {}
            stats = data.get("merged") or {}
        except Exception as e:
            logger.warning(f"[stats-dashboard] fetch failed: {e}")
            stats = {}
    if not stats:
        return jsonify({"status": "skipped", "reason": "no-stats"}), 200
    ok = discord_bridge.post_stats_dashboard(stats)
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


def _fetch_macro_events_week() -> list[dict]:
    """Pull this-week high+medium impact USD events from ForexFactory."""
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=6.0,
            headers={"User-Agent": "NOVA-Algo-Bot/1.0"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        events = []
        for ev in data:
            ts = ev.get("date") or ""
            currency = (ev.get("country") or ev.get("currency") or "").upper()
            if currency not in ("USD", ""):
                continue
            impact = (ev.get("impact") or "").lower()
            if impact not in ("high", "medium"):
                continue
            time_part = ts[11:16] if len(ts) >= 16 else "—"
            date_part = ts[:10] if len(ts) >= 10 else "this week"
            try:
                day_label = datetime.fromisoformat(ts).strftime("%a · %b %d") if ts else date_part
            except Exception:
                day_label = date_part
            events.append({
                "date":  day_label,
                "time":  f"{time_part} ET",
                "title": ev.get("title", "Event"),
                "currency": currency or "USD",
                "impact": impact,
                "forecast": ev.get("forecast") or None,
                "previous": ev.get("previous") or None,
            })
        events.sort(key=lambda e: (e["date"], 0 if e["impact"] == "high" else 1, e["time"]))
        return events
    except Exception as e:
        logger.warning(f"[economic-calendar] fetch failed: {e}")
        return []


_link_codes: dict[str, dict] = {}  # code -> {discord_id, discord_name, expires_at}
_LINK_CODE_TTL_SEC = 600  # 10 minutes


def _purge_expired_link_codes():
    now = datetime.now(EST).timestamp()
    expired = [c for c, d in _link_codes.items() if d.get("expires_at", 0) < now]
    for c in expired:
        _link_codes.pop(c, None)


@app.route("/admin/link/issue", methods=["POST"])
def admin_link_issue():
    """Bot calls this with a Discord user ID. Returns a 6-digit linking code
    that the user pastes at novaalgo.org/portal/link-discord. Code expires in 10m."""
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}
    discord_id = str(body.get("discord_id", "")).strip()
    discord_name = str(body.get("discord_name", "")).strip()
    if not discord_id:
        return jsonify({"status": "bad-request", "message": "discord_id required"}), 400
    _purge_expired_link_codes()
    # Generate 6-digit numeric code, retry on collision
    import secrets
    for _ in range(8):
        code = f"{secrets.randbelow(10**6):06d}"
        if code not in _link_codes:
            break
    expires = datetime.now(EST).timestamp() + _LINK_CODE_TTL_SEC
    _link_codes[code] = {
        "discord_id": discord_id,
        "discord_name": discord_name,
        "expires_at": expires,
    }
    return jsonify({"status": "ok", "code": code, "expires_in_sec": _LINK_CODE_TTL_SEC}), 200


@app.route("/admin/link/consume", methods=["POST"])
def admin_link_consume():
    """Site calls this with a code. Returns the discord_id if valid + not expired,
    then deletes the code (one-shot)."""
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip()
    if not code:
        return jsonify({"status": "bad-request", "message": "code required"}), 400
    _purge_expired_link_codes()
    entry = _link_codes.pop(code, None)
    if not entry:
        return jsonify({"status": "not-found"}), 404
    return jsonify({
        "status": "ok",
        "discord_id": entry["discord_id"],
        "discord_name": entry.get("discord_name", ""),
    }), 200


@app.route("/admin/halt", methods=["POST"])
def admin_halt():
    """Manually halt fanout for the rest of the session. Cleared at next day rollover."""
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    state["manual_halt"] = True
    logger.warning("[admin] pipeline manually halted")
    if discord_bridge:
        try:
            discord_bridge.post_status(
                "🛑 NOVA pipeline manually halted",
                level="warn",
                fields=[{"name": "Auto-clears", "value": "Next day rollover (00:00 ET)", "inline": True}],
            )
        except Exception:
            pass
    return jsonify({"status": "halted", "manual_halt": True}), 200


@app.route("/admin/unhalt", methods=["POST"])
def admin_unhalt():
    """Clear the manual halt early."""
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    state["manual_halt"] = False
    logger.info("[admin] manual halt cleared")
    return jsonify({"status": "unhalted", "manual_halt": False}), 200


CONCEPT_BANK_PATH = os.path.join(os.path.dirname(__file__), "nova-algo-discord", "content", "concepts.json")
CONCEPT_CURSOR_PATH = os.path.join(os.path.dirname(__file__), ".concept_cursor.json")


def _next_concept() -> dict | None:
    """Pick the next concept from the rotation file. Cursor advances each call."""
    try:
        with open(CONCEPT_BANK_PATH, "r", encoding="utf-8") as f:
            bank = json.load(f)
    except Exception as e:
        logger.warning(f"[concept] bank load failed: {e}")
        return None
    if not bank:
        return None
    try:
        with open(CONCEPT_CURSOR_PATH, "r", encoding="utf-8") as f:
            cursor = int(json.load(f).get("idx", 0))
    except Exception:
        cursor = 0
    pick = bank[cursor % len(bank)]
    try:
        with open(CONCEPT_CURSOR_PATH, "w", encoding="utf-8") as f:
            json.dump({"idx": (cursor + 1) % len(bank)}, f)
    except Exception:
        pass
    return pick


@app.route("/discord/concept/post", methods=["POST"])
def discord_concept_post():
    """Weekly concept-of-the-week post → #concept-of-the-week.

    With a body: posts the supplied title/body.
    Empty body: pulls the next concept from content/concepts.json rotation.
    """
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}
    title = body.get("title")
    content = body.get("body") or body.get("content")
    takeaway = body.get("takeaway")

    if not title or not content:
        # Fall back to the rotation
        c = _next_concept()
        if not c:
            return jsonify({"status": "skipped", "reason": "no-concepts-available"}), 200
        title = c.get("title")
        content = c.get("body")
        takeaway = c.get("takeaway") or takeaway

    ok = discord_bridge.post_concept_of_the_week(
        title=title,
        body=content,
        takeaway=takeaway,
        week_label=body.get("week_label"),
    )
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


@app.route("/discord/milestones/post", methods=["POST"])
def discord_milestones_post():
    """Event-triggered milestone post → #milestones."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    body = request.get_json(silent=True) or {}
    kind = body.get("kind", "milestone")
    title = body.get("title")
    if not title:
        return jsonify({"status": "bad-request", "message": "title required"}), 400
    ok = discord_bridge.post_milestone(
        kind=kind,
        title=title,
        body=body.get("body", ""),
        color=body.get("color"),
    )
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok)}), 200


@app.route("/discord/economic-calendar/post", methods=["POST"])
def discord_economic_calendar_post():
    """Weekly macro digest → #economic-calendar. Sunday 6pm ET via cron."""
    if not discord_bridge:
        return jsonify({"status": "error", "message": "discord_bridge unavailable"}), 503
    authed, reason = _webhook_auth_ok(request)
    if not authed:
        return jsonify({"status": "unauthorized", "message": reason}), 401
    events = _fetch_macro_events_week()
    week_label = datetime.now(EST).strftime("Week of %b %d, %Y")
    ok = discord_bridge.post_economic_calendar(events, week_label=week_label)
    return jsonify({"status": "ok" if ok else "skipped", "posted": bool(ok), "count": len(events)}), 200


@app.route("/signals/recent", methods=["GET"])
def signals_recent():
    """
    Return the most recent NOVA signals (ring buffer, newest first).
    Used by the founder dashboard at novaalgo.org/portal.
    Optional query param `limit` caps the response (default 10, max 50).
    """
    try:
        limit = int(request.args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, SIGNAL_RING_CAP))
    return jsonify({
        "status":  "ok",
        "count":   len(state["last_signals"]),
        "signals": state["last_signals"][:limit],
    }), 200


# ═══════════════════════════════════════════════════════════════════════════
# Trading Commander — agent hierarchy (lazy singleton)
# ═══════════════════════════════════════════════════════════════════════════

_commander = None

def _get_commander():
    """Lazy-init so importing app.py for tests doesn't boot threads."""
    global _commander
    if _commander is None:
        from nova_trading_agents import TradingCommander
        _commander = TradingCommander(
            gate_fn          = evaluate_gates,
            build_tp_payload = build_traderspost_payload,
            discord_url      = os.environ.get("NOVA_DISCORD_WEBHOOK_URL", ""),
        )
        logger.info("[TradingCommander] initialized — Observability + Dispatcher online")
    return _commander


@app.route("/fire", methods=["GET", "POST"])
def fire_pending():
    """
    One-tap manual fire. Sir receives a Discord DM with a URL like:
        https://.../fire?token=<16b>&sig=<16b>
    Tapping it POPs the queued trade off the pending queue and fires
    TradersPost once. Token expires in 30 minutes; single-use.
    """
    token = request.args.get("token") or (request.get_json(silent=True) or {}).get("token", "")
    sig   = request.args.get("sig")   or (request.get_json(silent=True) or {}).get("sig",   "")
    if not token or not sig:
        return jsonify({"ok": False, "status": "invalid", "message": "token and sig required"}), 400

    commander = _get_commander()
    result    = commander.fire_pending(token, sig)
    code      = 200 if result.get("ok") else 400

    # Keep the response phone-friendly — minimal HTML so a tap from Discord
    # shows something readable rather than raw JSON.
    if "text/html" in (request.headers.get("Accept") or ""):
        color = "#00C853" if result.get("ok") else "#E53E3E"
        html  = (
            f"<html><body style='font-family:system-ui;background:#0A1929;color:#d0d8e8;"
            f"padding:40px;text-align:center'>"
            f"<h1 style='color:{color}'>{result.get('status','?').upper()}</h1>"
            f"<p>{result.get('message','')}</p>"
            f"<p style='color:#888;font-size:12px'>signal {result.get('signal_id','—')}</p>"
            f"</body></html>"
        )
        return html, code, {"Content-Type": "text/html"}
    return jsonify(result), code


@app.route("/agents/ledger", methods=["GET"])
def agents_ledger():
    """Rolling ledger of every commander decision — last 50 by default."""
    from nova_trading_agents import get_ledger
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except Exception:
        limit = 50
    return jsonify({"entries": get_ledger(limit)}), 200


# ═══════════════════════════════════════════════════════════════════════════
# News Agent — #economic-calendar Discord relay
# ═══════════════════════════════════════════════════════════════════════════

_news_started = False

def _start_news_agent_once():
    """Start the news daemon exactly once, on first request. Flask 3.x has
    no before_first_request hook, so we gate on a module-level flag."""
    global _news_started
    if _news_started:
        return
    _news_started = True
    try:
        from nova_news_agent import get_agent
        agent = get_agent()
        agent.start()
        logger.info("[news-agent] daemon started")
    except Exception as e:
        logger.error(f"[news-agent] failed to start: {e}")


@app.before_request
def _boot_news_agent():
    _start_news_agent_once()


@app.route("/news/weekly", methods=["POST", "GET"])
def news_weekly():
    """Manual trigger — fires the weekly preview embed right now. Returns the
    actual post result so we can see if Discord accepted it."""
    from nova_news_agent import get_agent
    agent = get_agent()
    events = agent.events_for_week(datetime.now(tz=EST))
    embed  = agent.fmt_weekly(events, datetime.now(tz=EST))
    posted = agent._post(embed) if embed else False
    if posted:
        agent._last_weekly_at = datetime.now(tz=EST)
    return jsonify({
        "ok":              posted,
        "fired":           "weekly",
        "events_count":    len(events),
        "discord_url_set": bool(agent.discord_url),
        "discord_url_len": len(agent.discord_url or ""),
        "embed_built":     bool(embed),
    }), 200


@app.route("/news/status", methods=["GET"])
def news_status():
    """Diagnostic — what does the news agent actually see?"""
    import os as _os
    from nova_news_agent import get_agent
    agent = get_agent()
    env_url = _os.environ.get("NOVA_NEWS_DISCORD_WEBHOOK_URL", "")
    return jsonify({
        "agent_discord_url_set":    bool(agent.discord_url),
        "agent_discord_url_len":    len(agent.discord_url or ""),
        "env_var_set":              bool(env_url),
        "env_var_len":              len(env_url),
        "env_var_starts_with":      env_url[:40] if env_url else "",
        "last_weekly_at":           agent._last_weekly_at.isoformat() if agent._last_weekly_at else None,
        "last_daily_at":            agent._last_daily_at.isoformat() if agent._last_daily_at else None,
        "events_cached":            len(agent._events_cache),
    }), 200


@app.route("/news/daily", methods=["POST", "GET"])
def news_daily():
    """Manual trigger — fires today's daily menu embed."""
    from nova_news_agent import get_agent
    agent = get_agent()
    agent.maybe_post_daily(force=True)
    return jsonify({"ok": True, "fired": "daily"}), 200


@app.route("/news/scan", methods=["POST", "GET"])
def news_scan():
    """Manual trigger — runs one pre/post scan cycle."""
    from nova_news_agent import get_agent
    agent = get_agent()
    agent.scan_pre_and_post()
    return jsonify({"ok": True, "fired": "scan"}), 200


# ═══════════════════════════════════════════════════════════════════════════
# Headlines Agent — #live-headlines Discord relay
# ═══════════════════════════════════════════════════════════════════════════

_headlines_started = False

def _start_headlines_agent_once():
    global _headlines_started
    if _headlines_started:
        return
    _headlines_started = True
    try:
        from nova_headlines_agent import get_agent as _hl_get
        agent = _hl_get()
        agent.start()
        logger.info("[headlines-agent] daemon started")
    except Exception as e:
        logger.error(f"[headlines-agent] failed to start: {e}")


@app.before_request
def _boot_headlines_agent():
    _start_headlines_agent_once()


@app.route("/headlines/status", methods=["GET"])
def headlines_status():
    import os as _os
    from nova_headlines_agent import get_agent as _hl_get, SOURCES
    agent   = _hl_get()
    env_url = _os.environ.get("NOVA_HEADLINES_DISCORD_WEBHOOK_URL", "")
    return jsonify({
        "agent_discord_url_set": bool(agent.discord_url),
        "env_var_set":           bool(env_url),
        "sources":               [s.name for s in SOURCES],
        "seen_counts":           {name: len(q) for name, q in agent._seen_per_source.items()},
        "first_cycle":           agent._first_cycle,
        "poll_live_s":           agent.poll_live,
        "poll_quiet_s":          agent.poll_quiet,
        "max_per_cycle":         agent.max_per_cycle,
    }), 200


@app.route("/headlines/fire", methods=["GET", "POST"])
def headlines_fire():
    """Manual trigger — runs one polling cycle and force-posts fresh items."""
    from nova_headlines_agent import get_agent as _hl_get
    agent  = _hl_get()
    posted = agent.tick(force_post=True)
    return jsonify({"ok": True, "posted": posted}), 200


# ═══════════════════════════════════════════════════════════════════════════
# Watchlist Agent — #watchlist Discord relay
# ═══════════════════════════════════════════════════════════════════════════

_watchlist_started = False

def _start_watchlist_agent_once():
    global _watchlist_started
    if _watchlist_started:
        return
    _watchlist_started = True
    try:
        from nova_watchlist_agent import get_agent as _wl_get
        agent = _wl_get()
        agent.start()
        logger.info("[watchlist-agent] daemon started")
    except Exception as e:
        logger.error(f"[watchlist-agent] failed to start: {e}")


@app.before_request
def _boot_watchlist_agent():
    _start_watchlist_agent_once()


@app.route("/watchlist/status", methods=["GET"])
def watchlist_status():
    import os as _os
    from nova_watchlist_agent import get_agent as _wl_get, TICKERS
    agent   = _wl_get()
    env_url = _os.environ.get("NOVA_WATCHLIST_DISCORD_WEBHOOK_URL", "")
    return jsonify({
        "env_var_set":        bool(env_url),
        "agent_url_set":      bool(agent.discord_url),
        "tickers":            [t.display for t in TICKERS],
        "last_morning":       str(agent._last_morning_date)     if agent._last_morning_date     else None,
        "last_intraday_hour": agent._last_intraday_hour,
        "last_eod":           str(agent._last_eod_date)         if agent._last_eod_date         else None,
        "last_big_moves":     {k: v.isoformat() for k, v in agent._last_big_move_at.items()},
    }), 200


def _watchlist_run(kind: str):
    """Shared helper: run a fire, return a detailed result for debugging."""
    from nova_watchlist_agent import get_agent as _wl_get
    agent = _wl_get()
    quotes = agent._fetch_quotes()
    if not quotes:
        return jsonify({
            "ok": False, "kind": kind, "reason": "yfinance fetch returned zero quotes",
            "url_set": bool(agent.discord_url),
        }), 200
    if kind == "morning":
        embed = agent.fmt_morning(quotes)
    elif kind == "intraday":
        embed = agent.fmt_intraday(quotes)
    elif kind == "eod":
        embed = agent.fmt_eod(quotes)
    elif kind == "weekend":
        embed = agent.fmt_weekend_crypto(quotes)
    else:
        return jsonify({"ok": False, "kind": kind, "reason": "unknown kind"}), 400
    posted = agent._post(embed) if embed else False
    return jsonify({
        "ok":         posted,
        "kind":       kind,
        "quotes":     len(quotes),
        "url_set":    bool(agent.discord_url),
        "embed_built": bool(embed),
    }), 200


@app.route("/watchlist/morning", methods=["GET", "POST"])
def watchlist_morning():
    return _watchlist_run("morning")


@app.route("/watchlist/intraday", methods=["GET", "POST"])
def watchlist_intraday():
    return _watchlist_run("intraday")


@app.route("/watchlist/eod", methods=["GET", "POST"])
def watchlist_eod():
    return _watchlist_run("eod")


@app.route("/watchlist/weekend", methods=["GET", "POST"])
def watchlist_weekend():
    return _watchlist_run("weekend")


# ═══════════════════════════════════════════════════════════════════════════
# Daily Bias Agent — #daily-bias Discord relay
# ═══════════════════════════════════════════════════════════════════════════

_bias_started = False

def _start_bias_agent_once():
    global _bias_started
    if _bias_started:
        return
    _bias_started = True
    try:
        from nova_bias_agent import get_agent as _bias_get
        _bias_get().start()
        logger.info("[bias-agent] daemon started")
    except Exception as e:
        logger.error(f"[bias-agent] failed to start: {e}")


@app.before_request
def _boot_bias_agent():
    _start_bias_agent_once()


@app.route("/bias/status", methods=["GET"])
def bias_status():
    import os as _os
    from nova_bias_agent import get_agent as _bias_get
    agent   = _bias_get()
    env_url = _os.environ.get("NOVA_BIAS_DISCORD_WEBHOOK_URL", "")
    return jsonify({
        "env_var_set":    bool(env_url),
        "agent_url_set":  bool(agent.discord_url),
        "last_post_date": str(agent._last_post_date) if agent._last_post_date else None,
    }), 200


@app.route("/bias/fire", methods=["GET", "POST"])
def bias_fire():
    from nova_bias_agent import get_agent as _bias_get
    agent = _bias_get()
    ctx   = agent._fetch_levels()
    errors = ctx.pop("_errors", []) if ctx else []
    # We now post even with partial data — bias agent gracefully handles missing fields
    keys = sorted(ctx.keys()) if ctx else []
    if not ctx:
        return jsonify({"ok": False, "reason": "no market data", "errors": errors[:5]}), 200
    bias  = agent.compute_bias(ctx)
    embed = agent.fmt_embed(ctx, bias)
    posted = agent._post(embed)
    if posted:
        agent._last_post_date = datetime.now(tz=EST).date()
    return jsonify({
        "ok":       posted,
        "bias":     bias["bias"],
        "strength": bias["strength"],
        "ctx_keys": keys,
        "errors":   errors[:5],
    }), 200


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("NOVA webhook server starting...")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
