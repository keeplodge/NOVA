"""
NOVA Neural Brain — Reflection Agent

Nightly loop (10pm EST) that calls local Ollama to synthesise new
"insight" memories from the last 24h of raw data. Each insight lands
in pending_insights for Sir to approve or reject before it graduates
into the main memories table. Insights at or above AUTO_APPROVE_THRESHOLD
bypass the gate and graduate directly.
"""
from __future__ import annotations
import asyncio
import json
import math
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
import httpx

import os

DB_PATH      = Path(__file__).parent / "brain.db"
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Reflector model is configurable. Default is llama3.2:3b (~2 GiB footprint)
# because Sir's current system can't load llama3:8b under typical RAM load.
# Override via env var NOVA_REFLECTOR_MODEL — e.g. "llama3" once RAM frees up,
# or "phi3:mini" / "qwen2.5:3b" for other light options.
OLLAMA_MODEL = os.environ.get("NOVA_REFLECTOR_MODEL", "llama3.2:3b")
EST          = ZoneInfo("America/New_York")

REFLECTION_HOUR        = 22     # 10pm EST
AUTO_APPROVE_THRESHOLD = 0.85   # >= this → auto-graduate to memories
LOOKBACK_HOURS         = 24

VALID_CATS = {"trading", "keeplodge", "personal", "ideas", "nova", "general"}

TRADE_LOG_DIR = Path(r"C:\Users\User\nova\nova-brain\01_Trade_Logs")


REFLECTION_PROMPT = """You are NOVA's reflective layer — the part of the brain that thinks overnight.

Below is the last 24 hours of raw data. Produce 1 to 3 INSIGHTS: cross-source
patterns, anomalies, or decisions that should survive into long-term memory.
Skip shallow observations — if signal is low, return [].

RECENT MEMORIES:
{memories}

RECENT TRADES (from Obsidian logs):
{trades}

RULES:
- Return JSON ONLY. No prose, no markdown, no backticks.
- Each insight cites the memory IDs it draws from in "sources".
- confidence 0.0 to 1.0. Be honest — below 0.6 means uncertain.
- category must be one of: trading, keeplodge, personal, ideas, nova, general.

Output schema:
[
  {{
    "summary": "one line under 120 chars",
    "content": "the insight, 2 to 4 sentences",
    "category": "trading",
    "sources": ["mem_id_1", "mem_id_2"],
    "confidence": 0.85
  }}
]

If nothing is worth remembering, return: []
"""


# ── Schema ──────────────────────────────────────────────────────────────────

async def init_pending_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_insights (
                id          TEXT PRIMARY KEY,
                content     TEXT NOT NULL,
                summary     TEXT,
                category    TEXT DEFAULT 'general',
                sources     TEXT DEFAULT '[]',
                confidence  REAL DEFAULT 0.0,
                created_at  REAL,
                status      TEXT DEFAULT 'pending'
            )
        """)
        await db.commit()


# ── Context gathering ──────────────────────────────────────────────────────

async def _recent_memories(hours: int = LOOKBACK_HOURS) -> list[dict]:
    cutoff = time.time() - hours * 3600
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT id, summary, content, category
            FROM memories
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT 20
        """, (cutoff,))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _recent_trade_logs(hours: int = LOOKBACK_HOURS) -> list[str]:
    if not TRADE_LOG_DIR.exists():
        return []
    cutoff = time.time() - hours * 3600
    entries = []
    for f in sorted(TRADE_LOG_DIR.glob("*.md"), reverse=True):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            entries.append(f"--- {f.name} ---\n{text[:500]}")
        except Exception:
            continue
        if len(entries) >= 5:
            break
    return entries


def _format_memories(mems: list[dict]) -> str:
    if not mems:
        return "(none)"
    return "\n".join(
        f"[{m['id']}] ({m['category']}) {m.get('summary') or (m.get('content') or '')[:140]}"
        for m in mems
    )


# ── Ollama ─────────────────────────────────────────────────────────────────

async def _call_ollama(prompt: str, timeout: float = 600.0) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{OLLAMA_URL}/api/generate", json={
            "model":   OLLAMA_MODEL,
            "prompt":  prompt,
            "stream":  False,
            "options": {"temperature": 0.6, "num_predict": 512},
        })
        r.raise_for_status()
        return r.json().get("response", "")


def _extract_json_array(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


# ── Persistence ─────────────────────────────────────────────────────────────

def _sphere_pos():
    theta = random.uniform(0, 2 * math.pi)
    phi   = math.acos(random.uniform(-1, 1))
    r     = 2.5 + random.uniform(-0.4, 0.4)
    return (
        r * math.sin(phi) * math.cos(theta),
        r * math.sin(phi) * math.sin(theta),
        r * math.cos(phi),
    )


async def _promote_to_memory(mid: str, content: str, summary: str,
                             category: str, sources: list[str],
                             tags_extra: list[str] | None = None) -> dict:
    """Insert a new row in memories and return the memory dict."""
    tags = list(dict.fromkeys(["reflection", category, *(tags_extra or [])]))[:6]
    x, y, z = _sphere_pos()
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO memories (id, content, summary, category, tags,
                                  connections, created_at, updated_at, x, y, z)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (mid, content, summary, category, json.dumps(tags),
              json.dumps(sources), now, now, x, y, z))
        await db.commit()
    return {
        "id":         mid,
        "content":    content,
        "summary":    summary,
        "category":   category,
        "tags":       tags,
        "connections": sources,
        "x": x, "y": y, "z": z,
        "created_at": now,
    }


async def _persist_insight(insight: dict) -> dict:
    mid     = str(uuid.uuid4())[:8]
    now     = time.time()
    content = str(insight.get("content", ""))[:2000].strip()
    summary = str(insight.get("summary", ""))[:200].strip()
    cat     = str(insight.get("category", "general")).lower().strip()
    sources = insight.get("sources") or []
    if not isinstance(sources, list):
        sources = []
    try:
        conf = float(insight.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0

    if not content:
        return {"ok": False, "reason": "empty content"}
    if cat not in VALID_CATS:
        cat = "general"

    if conf >= AUTO_APPROVE_THRESHOLD:
        mem = await _promote_to_memory(mid, content, summary, cat, sources, ["auto"])
        return {"ok": True, "id": mid, "status": "approved", "memory": mem,
                "confidence": conf}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO pending_insights (id, content, summary, category,
                                          sources, confidence, created_at, status)
            VALUES (?,?,?,?,?,?,?,'pending')
        """, (mid, content, summary, cat, json.dumps(sources), conf, now))
        await db.commit()
    return {"ok": True, "id": mid, "status": "pending", "confidence": conf}


# ── Approve / reject helpers (called from HTTP endpoints) ───────────────────

async def list_pending() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM pending_insights
            WHERE status = 'pending'
            ORDER BY created_at DESC
        """)
        rows = await cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["sources"] = json.loads(d.get("sources") or "[]")
        except Exception:
            d["sources"] = []
        out.append(d)
    return out


async def approve_insight(iid: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM pending_insights WHERE id = ? AND status = 'pending'",
            (iid,),
        )
        row = await cur.fetchone()
    if not row:
        return {"ok": False, "reason": "not found or already acted on"}

    d = dict(row)
    try:
        sources = json.loads(d.get("sources") or "[]")
    except Exception:
        sources = []

    mem = await _promote_to_memory(
        mid=iid,
        content=d["content"],
        summary=d.get("summary") or "",
        category=d.get("category") or "general",
        sources=sources,
        tags_extra=["approved"],
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_insights SET status = 'approved' WHERE id = ?", (iid,)
        )
        await db.commit()

    return {"ok": True, "id": iid, "memory": mem}


async def reject_insight(iid: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE pending_insights SET status = 'rejected' WHERE id = ? AND status = 'pending'",
            (iid,),
        )
        await db.commit()
        if cur.rowcount == 0:
            return {"ok": False, "reason": "not found or already acted on"}
    return {"ok": True, "id": iid}


# ── One reflection pass ─────────────────────────────────────────────────────

async def run_reflection() -> dict:
    await init_pending_table()

    mems   = await _recent_memories()
    trades = _recent_trade_logs()

    prompt = REFLECTION_PROMPT.format(
        memories=_format_memories(mems),
        trades=("\n\n".join(trades) if trades else "(no trades logged in last 24h)"),
    )

    print(f"[reflector] calling {OLLAMA_MODEL} with {len(mems)} memories, {len(trades)} trades")
    try:
        raw = await _call_ollama(prompt)
    except Exception as e:
        return {
            "ok": False,
            "reason": f"ollama error: {type(e).__name__}: {e}",
            "model": OLLAMA_MODEL,
            "count": 0,
        }

    insights = _extract_json_array(raw)
    if not insights:
        return {"ok": True, "count": 0, "approved": 0, "pending": 0,
                "reason": "no insights emitted", "raw_preview": raw[:200]}

    approved, pending, persisted = 0, 0, []
    for ins in insights:
        result = await _persist_insight(ins)
        if not result.get("ok"):
            continue
        persisted.append(result)
        if result.get("status") == "approved":
            approved += 1
        else:
            pending += 1

    return {
        "ok":         True,
        "count":      len(persisted),
        "approved":   approved,
        "pending":    pending,
        "raw_count":  len(insights),
        "insights":   persisted,
        "ran_at_est": datetime.now(tz=EST).isoformat(),
    }


# ── Scheduler ──────────────────────────────────────────────────────────────

async def scheduler_loop():
    """Fires run_reflection at REFLECTION_HOUR EST every day."""
    await init_pending_table()
    while True:
        now_est = datetime.now(tz=EST)
        target  = now_est.replace(hour=REFLECTION_HOUR, minute=0, second=0, microsecond=0)
        if target <= now_est:
            target = target + timedelta(days=1)
        wait_s = (target - now_est).total_seconds()
        print(f"[reflector] next reflection at {target.isoformat()} (sleeping {wait_s:.0f}s)")
        try:
            await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            return
        try:
            result = await run_reflection()
            print(f"[reflector] result: {result}")
        except Exception as e:
            print(f"[reflector] ERROR: {e}")
