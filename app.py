import json
import logging
import os
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TRADERSPOST_WEBHOOK_URL = os.environ["TRADERSPOST_WEBHOOK_URL"]
MAX_TRADES_PER_SESSION = 1
MAX_TRADES_PER_DAY     = 3
MAX_DAILY_LOSS         = 500.00  # USD

EST = ZoneInfo("America/New_York")

# ── Session windows (EST) ─────────────────────────────────────────────────────
SESSIONS = {
    "Asia":   {"start": (19, 0),  "end": (22, 0)},   # 7pm  – 10pm
    "London": {"start": (2,  0),  "end": (5,  0)},   # 2am  – 5am
    "NY_AM":  {"start": (8, 30),  "end": (11, 0)},   # 8:30am – 11am
}

# ── In-memory state ───────────────────────────────────────────────────────────
state = {
    "date":           None,
    "trades_today":   0,
    "daily_loss":     0.0,
    "session_trades": {},
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


def get_current_session(now: datetime) -> str | None:
    minutes = now.hour * 60 + now.minute
    for name, window in SESSIONS.items():
        start = window["start"][0] * 60 + window["start"][1]
        end   = window["end"][0]   * 60 + window["end"][1]
        if start <= minutes < end:
            return name
    return None


def validate_payload(data: dict) -> tuple[bool, str]:
    """Validate required fields from the TradingView alert."""
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
    """
    Map the incoming TradingView alert to the clean TradersPost format.

    Expected TradingView fields:
        ticker    — "NQ1!"
        action    — "buy" | "sell"
        quantity  — 1
        orderType — "market"
        price     — 19850.25
        comment   — "first" | "cont"
        session   — TradingView timestamp (logged only, not forwarded)

    TradersPost output fields:
        ticker, action, price, quantity, orderType, sentiment, comment
    """
    # Log the TradingView timestamp for reference — not forwarded to TradersPost
    tv_timestamp = data.get("session", "")
    if tv_timestamp:
        logger.info(f"TradingView session timestamp: {tv_timestamp}")

    # Enrich comment with server-detected session name
    # e.g. "first" → "first | NY_AM",  "cont" → "cont | Asia"
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


def forward_to_traderspost(payload: dict) -> tuple[bool, str]:
    """Forward the mapped payload to TradersPost."""
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

    # 4. Session check
    session = get_current_session(now)
    if session is None:
        msg = f"Signal rejected — outside all trading sessions ({now.strftime('%H:%M %Z')})"
        logger.warning(msg)
        return jsonify({"status": "rejected", "message": msg}), 200

    logger.info(f"Active session: {session}")

    # 5. Per-session trade limit
    session_count = state["session_trades"].get(session, 0)
    if session_count >= MAX_TRADES_PER_SESSION:
        msg = f"Signal rejected — max {MAX_TRADES_PER_SESSION} trade(s) already taken in {session} session"
        logger.warning(msg)
        return jsonify({"status": "rejected", "message": msg}), 200

    # 6. Daily trade limit
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        msg = f"Signal rejected — daily trade limit of {MAX_TRADES_PER_DAY} reached"
        logger.warning(msg)
        return jsonify({"status": "rejected", "message": msg}), 200

    # 7. Daily loss limit
    if state["daily_loss"] >= MAX_DAILY_LOSS:
        msg = f"Signal rejected — daily loss limit of ${MAX_DAILY_LOSS:.2f} reached (current: ${state['daily_loss']:.2f})"
        logger.warning(msg)
        return jsonify({"status": "rejected", "message": msg}), 200

    # 8. Build clean TradersPost payload and forward
    tp_payload = build_traderspost_payload(data, session)
    success, tp_response = forward_to_traderspost(tp_payload)
    if not success:
        logger.error(f"Failed to forward signal: {tp_response}")
        return jsonify({"status": "error", "message": tp_response}), 502

    # 9. Update state
    state["trades_today"] += 1
    state["session_trades"][session] = session_count + 1

    logger.info(
        f"Signal forwarded — session: {session} | "
        f"session trades: {state['session_trades'][session]}/{MAX_TRADES_PER_SESSION} | "
        f"day trades: {state['trades_today']}/{MAX_TRADES_PER_DAY} | "
        f"daily loss: ${state['daily_loss']:.2f}/${MAX_DAILY_LOSS:.2f}"
    )

    return jsonify({
        "status":         "ok",
        "message":        "Signal accepted and forwarded",
        "session":        session,
        "trades_today":   state["trades_today"],
        "session_trades": state["session_trades"][session],
        "daily_loss":     state["daily_loss"],
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
        "status":    "ok",
        "daily_loss": state["daily_loss"],
        "limit":      MAX_DAILY_LOSS,
        "remaining":  max(0.0, MAX_DAILY_LOSS - state["daily_loss"]),
    }), 200


# ── Status endpoint ───────────────────────────────────────────────────────────

@app.route("/status", methods=["GET"])
def status():
    reset_daily_state_if_new_day()
    now     = datetime.now(tz=EST)
    session = get_current_session(now)
    return jsonify({
        "time_est":       now.strftime("%Y-%m-%d %H:%M:%S"),
        "active_session": session or "None",
        "trades_today":   state["trades_today"],
        "session_trades": state["session_trades"],
        "daily_loss":     state["daily_loss"],
        "loss_limit":     MAX_DAILY_LOSS,
        "loss_remaining": max(0.0, MAX_DAILY_LOSS - state["daily_loss"]),
    }), 200


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("NOVA webhook server starting...")
    app.run(host="0.0.0.0", port=5000, debug=False)
