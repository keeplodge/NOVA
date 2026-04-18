import json
import logging
import os
import glob
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TRADERSPOST_WEBHOOK_URL = os.environ.get("TRADERSPOST_WEBHOOK_URL", "")
MAX_TRADES_PER_SESSION  = 1
MAX_TRADES_PER_DAY      = 3
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
EVAL_ACCOUNTS = {
    "apex_50k":  {"label": "Apex 50K",   "current": 48598.30,  "target": 53000.00},
    "apex_100k": {"label": "Apex 100K",  "current": 100000.00, "target": 106000.00},
    "lucid_50k": {"label": "Lucid 50K",  "current": 50000.00,  "target": 53000.00},
}

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
SESSIONS = {
    "Asia":   {"start": (19, 0),  "end": (23, 0)},
    "London": {"start": (2,  0),  "end": (5,  0)},
    "NY_AM":  {"start": (8, 30),  "end": (11, 0)},
}

# Crypto tickers — accepted on weekends when futures are closed
CRYPTO_TICKERS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BTCUSD", "ETHUSD", "SOLUSD", "XBTUSD"}

# ── In-memory state ───────────────────────────────────────────────────────────
state = {
    "date":            None,
    "trades_today":    0,
    "daily_loss":      0.0,
    "session_trades":  {},
    "last_signal":     None,   # {"action", "price", "session", "ticker"}
    "open_positions":  {},     # keyed by ticker → full signal dict at entry
}

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
        # open_positions is intentionally NOT reset on day rollover — a position
        # held across a day boundary (rare but possible) should survive. It's
        # cleared only by /close, /report-result, or an explicit admin call.


def get_current_session(now: datetime) -> str | None:
    minutes = now.hour * 60 + now.minute
    for name, window in SESSIONS.items():
        start = window["start"][0] * 60 + window["start"][1]
        end   = window["end"][0]   * 60 + window["end"][1]
        if start <= minutes < end:
            return name
    return None


def validate_payload(data: dict) -> tuple[bool, str]:
    for field in ("ticker", "action", "price"):
        if field not in data:
            return False, f"Missing required field: '{field}'"

    if data["action"] not in ("buy", "sell"):
        return False, f"Invalid action '{data['action']}' — must be 'buy' or 'sell'"

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

    payload = {
        "ticker":    data["ticker"].upper().strip(),
        "action":    data["action"],
        "price":     float(data["price"]),
        "quantity":  int(data.get("quantity", 1)),
        "orderType": data.get("orderType", "market"),
        "sentiment": sentiment_map[data["action"]],
        "comment":   comment,
    }

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

SESSION_DISPLAY = {"Asia": "Asia", "London": "London", "NY_AM": "NY AM"}
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

def evaluate_gates(ticker: str, grade: str | None, now: datetime) -> tuple[bool, str, dict]:
    """
    Run every risk gate. Returns (ok, reason_if_blocked, gate_state).
    gate_state is always populated for dry-run reporting.
    """
    is_crypto  = ticker.upper() in CRYPTO_TICKERS
    is_weekend = now.weekday() >= 5
    session    = get_current_session(now)

    # Weekend crypto exemption: weekday discipline stays tight, but on Sat/Sun
    # crypto signals outside Asia/London/NY_AM get their own "Weekend_Crypto"
    # lane so the Pine weekend crypto study can actually execute. All other
    # gates (grade, 1/session, 3/day, $500 loss cap) still apply.
    if is_weekend and is_crypto and session is None:
        session = "Weekend_Crypto"

    session_count = state["session_trades"].get(session, 0) if session else 0

    gate_state = {
        "now_est":         now.strftime("%Y-%m-%d %H:%M:%S"),
        "session":         session,
        "is_weekend":      is_weekend,
        "is_crypto":       is_crypto,
        "session_trades":  session_count,
        "trades_today":    state["trades_today"],
        "daily_loss":      state["daily_loss"],
        "open_position":   ticker.upper() in state["open_positions"],
        "grade":           grade,
    }

    if is_weekend and not is_crypto:
        return False, f"Rejected — futures market closed on weekend ({ticker})", gate_state
    if session is None:
        return False, f"Rejected — outside all trading sessions ({now.strftime('%H:%M %Z')})", gate_state
    if session_count >= MAX_TRADES_PER_SESSION:
        return False, f"Rejected — max {MAX_TRADES_PER_SESSION} trade(s) already in {session}", gate_state
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, f"Rejected — daily trade limit of {MAX_TRADES_PER_DAY} reached", gate_state
    if state["daily_loss"] >= MAX_DAILY_LOSS:
        return False, f"Rejected — daily loss limit of ${MAX_DAILY_LOSS:.2f} reached", gate_state
    if ticker.upper() in state["open_positions"]:
        return False, f"Rejected — position already open for {ticker.upper()}", gate_state

    return True, "OK", gate_state


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    now = datetime.now(tz=EST)
    logger.info(f"Incoming webhook — {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # 1. Parse JSON
    try:
        data = request.get_json(force=True, silent=False)
        if data is None:
            raise ValueError("Empty or non-JSON body")
    except Exception as e:
        logger.warning(f"Failed to parse request body: {e}")
        return jsonify({"status": "error", "message": "Invalid JSON payload"}), 400

    logger.info(f"Raw payload received: {json.dumps(data)}")

    # 2. Validate required fields
    valid, validation_message = validate_payload(data)
    if not valid:
        logger.warning(f"Payload validation failed: {validation_message}")
        return jsonify({"status": "error", "message": validation_message}), 400

    # 3. Reset daily state if new day
    reset_daily_state_if_new_day()

    # 4-7. Session / limit / loss gates
    ticker = data.get("ticker", "").upper().strip()
    grade  = data.get("grade")
    ok, reason, gate_state = evaluate_gates(ticker, grade, now)
    if not ok:
        logger.warning(reason)
        return jsonify({"status": "rejected", "message": reason, "gates": gate_state}), 200

    session = gate_state["session"]
    logger.info(f"Active session: {session}")

    # 8. Build clean TradersPost payload and forward
    tp_payload = build_traderspost_payload(data, session)
    success, tp_response = forward_to_traderspost(tp_payload)
    if not success:
        logger.error(f"Failed to forward signal: {tp_response}")
        return jsonify({"status": "error", "message": tp_response}), 502

    # 9. Update state
    state["trades_today"] += 1
    state["session_trades"][session] = gate_state["session_trades"] + 1
    state["last_signal"] = {
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
        "source":  "webhook",
    }
    state["open_positions"][ticker] = dict(state["last_signal"], opened_at=now.isoformat())

    logger.info(
        f"Signal forwarded — session: {session} | "
        f"session trades: {state['session_trades'][session]}/{MAX_TRADES_PER_SESSION} | "
        f"day trades: {state['trades_today']}/{MAX_TRADES_PER_DAY} | "
        f"daily loss: ${state['daily_loss']:.2f}/${MAX_DAILY_LOSS:.2f}"
    )

    # 10. Log trade to Obsidian
    log_path = log_trade_to_obsidian(data, session, now)

    return jsonify({
        "status":         "ok",
        "message":        "Signal accepted and forwarded",
        "session":        session,
        "trades_today":   state["trades_today"],
        "session_trades": state["session_trades"][session],
        "daily_loss":     state["daily_loss"],
        "trade_log":      log_path or "failed to write",
    }), 200


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
    state["last_signal"] = {
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
    }
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

        if outcome == "loss":
            reset_daily_state_if_new_day()
            state["daily_loss"] += RISK_PER_TRADE
            logger.info(f"Daily loss updated via /close: ${state['daily_loss']:.2f}/${MAX_DAILY_LOSS:.2f}")

        outcome_info = {"outcome": outcome, "exit_price": exit_px_f}

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

    # Update daily loss state if trade was a loss
    if outcome == "loss":
        reset_daily_state_if_new_day()
        state["daily_loss"] += RISK_PER_TRADE
        logger.info(f"Daily loss updated: ${state['daily_loss']:.2f}/${MAX_DAILY_LOSS:.2f}")

    # Clear tracked position if ticker supplied
    if ticker:
        state["open_positions"].pop(ticker, None)

    return jsonify({
        "status":     "ok",
        "message":    "Trade log updated",
        "outcome":    outcome,
        "exit_price": exit_price,
        "log_file":   os.path.basename(path),
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("NOVA webhook server starting...")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
