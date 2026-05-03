"""
nova_trade_db.py — Local SQLite persistence for NOVA trade intelligence.
"""
import sqlite3, os, json
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("NOVA_DB_PATH", r"C:\Users\User\nova\nova_brain.db")
EST = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    date          TEXT NOT NULL,
    time          TEXT NOT NULL,
    session       TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    direction     TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    sl_price      REAL,
    tp_price      REAL,
    be_price      REAL,
    exit_price    REAL,
    outcome       TEXT DEFAULT 'open',
    pnl           REAL,
    r_multiple    REAL,
    grade         TEXT,
    grade_score   INTEGER,
    sweep_type    TEXT,
    comment       TEXT,
    analysis      TEXT,
    raw_payload   TEXT
);

CREATE TABLE IF NOT EXISTS patterns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    session       TEXT,
    direction     TEXT,
    grade         TEXT,
    sweep_type    TEXT,
    win_rate      REAL,
    sample_size   INTEGER,
    details       TEXT,
    recommendation TEXT
);

CREATE TABLE IF NOT EXISTS parameter_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    param_name      TEXT NOT NULL,
    old_value       TEXT NOT NULL,
    new_value       TEXT NOT NULL,
    reason          TEXT,
    trades_analyzed INTEGER,
    win_rate_before REAL
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_trade(payload: dict, session: str, now: datetime) -> int:
    direction = "long" if payload.get("action") == "buy" else "short"
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO trades (
            created_at, date, time, session, ticker, direction,
            entry_price, sl_price, tp_price, be_price,
            outcome, grade, grade_score, sweep_type, comment, raw_payload
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now.isoformat(),
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        session,
        payload.get("ticker", "").upper(),
        direction,
        float(payload.get("price", 0)),
        float(payload["sl"]) if payload.get("sl") else None,
        float(payload["tp"]) if payload.get("tp") else None,
        float(payload["be"]) if payload.get("be") else None,
        "open",
        payload.get("grade"),
        # Pine v1.4.2 emits `grade_score`; legacy payloads use `score` — accept both.
        (int(payload["grade_score"]) if payload.get("grade_score") is not None
         else (int(payload["score"]) if payload.get("score") is not None else None)),
        payload.get("sweep"),
        payload.get("comment", ""),
        json.dumps(payload),
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def close_trade(trade_id: int, outcome: str, exit_price: float, analysis: str = None):
    pnl_map  = {"win": 1000.0, "loss": -500.0, "be": 0.0}
    r_map    = {"win": 2.0,    "loss": -1.0,   "be": 0.0}
    conn = get_conn()
    conn.execute("""
        UPDATE trades
        SET outcome=?, exit_price=?, pnl=?, r_multiple=?, analysis=?
        WHERE id=?
    """, (outcome, exit_price, pnl_map.get(outcome, 0.0),
          r_map.get(outcome, 0.0), analysis, trade_id))
    conn.commit()
    conn.close()


def get_last_open_trade() -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM trades WHERE outcome='open' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_trades(limit: int = 500, outcome: str = None) -> list[dict]:
    conn = get_conn()
    if outcome:
        rows = conn.execute(
            "SELECT * FROM trades WHERE outcome=? ORDER BY id DESC LIMIT ?",
            (outcome, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_conn()
    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN outcome='be'   THEN 1 ELSE 0 END) as breakevens,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(CASE WHEN outcome!='open' THEN r_multiple END), 2) as avg_r
        FROM trades WHERE outcome != 'open'
    """).fetchone()
    conn.close()
    d = dict(row)
    closed = (d["wins"] or 0) + (d["losses"] or 0) + (d["breakevens"] or 0)
    d["win_rate"] = round((d["wins"] or 0) / closed * 100, 1) if closed > 0 else 0.0
    return d
