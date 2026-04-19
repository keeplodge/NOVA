"""
NOVA Brain Bridge — the memory SDK used by every NOVA script that talks to the
Neural Brain (nova_assistant, analyst agent, pattern agent, evolution agent,
command classifier, dashboards).

All operations go through the FastAPI server at 127.0.0.1:7337 — no direct DB
access — so multiple callers stay consistent. Both async and sync-wrapped
variants are exposed; use the sync ones from schedulers and the async ones
from FastAPI/aiohttp contexts.

Conventions
-----------
Tags use the NOVA namespace prefix pattern:
  nova:briefing:YYYY-MM-DD
  nova:debrief:YYYY-MM-DD
  nova:insight:topic
  trading:trade:YYYY-MM-DD
  trading:session:YYYY-MM-DD:<session>
  trading:pattern:<date>

The `probuild:*` namespace is documented separately in that project's
architecture memory.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Iterable

import httpx

BRAIN_URL = "http://127.0.0.1:7337"
TIMEOUT   = 6.0


# ═══════════════════════════════════════════════════════════════════════════
# Async core
# ═══════════════════════════════════════════════════════════════════════════

async def is_online() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{BRAIN_URL}/health")
            return r.status_code == 200
    except Exception:
        return False


async def store_memory(
    content: str,
    category: str = "general",
    summary:  str = "",
    tags:     list[str] | None = None,
) -> dict | None:
    """Raw memory write. Returns the created memory dict or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(
                f"{BRAIN_URL}/memory",
                json={
                    "content":  content,
                    "category": category,
                    "summary":  summary,
                    "tags":     tags or [],
                },
            )
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def search(query: str, limit: int = 4) -> list[dict]:
    """FTS5 search across all memories."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{BRAIN_URL}/search", params={"q": query, "limit": limit})
            return r.json() if r.status_code == 200 else []
    except Exception:
        return []


async def recent(limit: int = 20) -> list[dict]:
    """Most recent memories (any category, most-recently-updated first)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{BRAIN_URL}/recent", params={"limit": limit})
            return r.json() if r.status_code == 200 else []
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════
# Sync wrappers — safe to call from sync threads (scheduler, command handler)
# ═══════════════════════════════════════════════════════════════════════════

def _sync(coro):
    try:
        loop = asyncio.new_event_loop()
        return loop.run_until_complete(coro)
    except Exception:
        return None
    finally:
        try:
            loop.close()
        except Exception:
            pass


def sync_online() -> bool:
    return bool(_sync(is_online()))


def sync_store(content, category="general", summary="", tags=None):
    return _sync(store_memory(content, category, summary, tags))


def sync_search(query: str, limit: int = 4) -> list[dict]:
    result = _sync(search(query, limit))
    return result or []


def sync_recent(limit: int = 20) -> list[dict]:
    result = _sync(recent(limit))
    return result or []


# ═══════════════════════════════════════════════════════════════════════════
# High-level helpers — use these in application code
# ═══════════════════════════════════════════════════════════════════════════

def remember(
    content:  str,
    category: str | None = None,
    summary:  str = "",
    tags:     Iterable[str] | None = None,
    heading:  str | None = None,
) -> dict | None:
    """
    Write a memory with smart defaults.
    - If category is None, auto-classifies from content.
    - If heading is given, it's prepended as the first tag (e.g. 'nova:briefing:2026-04-19').
    - If tags is None, auto-extracts up to 5 keyword tags from content.
    """
    cat = category or classify(content)
    final_tags = list(tags) if tags else extract_tags(content)
    if heading and heading not in final_tags:
        final_tags.insert(0, heading)
    return sync_store(content=content, category=cat, summary=summary, tags=final_tags)


def remember_briefing(date_str: str, content: str, summary: str = "") -> dict | None:
    return remember(
        content=content,
        category="nova",
        summary=summary or f"Morning briefing — {date_str}",
        heading=f"nova:briefing:{date_str}",
        tags=["briefing", "morning", date_str],
    )


def remember_debrief(date_str: str, content: str, summary: str = "") -> dict | None:
    return remember(
        content=content,
        category="nova",
        summary=summary or f"EOD debrief — {date_str}",
        heading=f"nova:debrief:{date_str}",
        tags=["debrief", "eod", date_str],
    )


def remember_trade(
    date_str: str,
    ticker:   str,
    action:   str,
    outcome:  str = "pending",
    session:  str = "",
    grade:    str = "",
    notes:    str = "",
) -> dict | None:
    lines = [
        f"Trade: {ticker} {action}",
        f"Date: {date_str}",
        f"Outcome: {outcome}",
    ]
    if session: lines.append(f"Session: {session}")
    if grade:   lines.append(f"Grade: {grade}")
    if notes:   lines.append(f"Notes: {notes}")
    body = "\n".join(lines)
    return remember(
        content=body,
        category="trading",
        summary=f"{ticker} {action} {outcome} — {session or date_str}",
        heading=f"trading:trade:{date_str}",
        tags=["trade", ticker.lower(), action.lower(), outcome.lower(), date_str],
    )


def remember_insight(
    topic:   str,
    content: str,
    category: str = "nova",
) -> dict | None:
    return remember(
        content=content,
        category=category,
        summary=f"Insight — {topic}",
        heading=f"{category}:insight:{topic}",
        tags=["insight", topic.lower()],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Retrieval — for context injection into Claude calls
# ═══════════════════════════════════════════════════════════════════════════

def recent_filtered(
    category: str | None = None,
    hours:    int = 72,
    limit:    int = 10,
) -> list[dict]:
    """Recent memories, optionally filtered by category + recency window."""
    pool = sync_recent(limit=max(limit * 3, 40))
    cutoff = time.time() - hours * 3600
    out = []
    for m in pool:
        if category and m.get("category") != category:
            continue
        if (m.get("created_at") or 0) < cutoff:
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def context_block(
    query:         str,
    category:      str | None = None,
    limit:         int = 4,
    include_recent: bool = True,
    header:        str = "RELEVANT MEMORIES",
) -> str:
    """
    Returns a ready-to-inject system-prompt block summarising the most relevant
    memories for `query`, optionally tinted with recent memories of the same
    category. Empty string if the Brain is offline or nothing relevant is
    found — safe to concatenate unconditionally.
    """
    chunks: list[dict] = []

    # FTS search by query
    hits = sync_search(query, limit=limit)
    if category:
        hits = [h for h in hits if h.get("category") == category]
    chunks.extend(hits[:limit])

    if include_recent:
        seen_ids = {c.get("id") for c in chunks}
        for m in recent_filtered(category=category, hours=72, limit=limit):
            if m.get("id") in seen_ids:
                continue
            chunks.append(m)
            if len(chunks) >= limit * 2:
                break

    if not chunks:
        return ""

    lines = [f"### {header}", ""]
    for m in chunks:
        date = ""
        ts = m.get("created_at")
        if ts:
            try:
                date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except Exception:
                date = ""
        cat = m.get("category", "general")
        summary = m.get("summary") or (m.get("content") or "")[:140]
        lines.append(f"- [{cat}{' ' + date if date else ''}] {summary}")
    lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Classification helpers — unchanged API, expanded heuristics
# ═══════════════════════════════════════════════════════════════════════════

_CAT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("trading",   ("trade","trading","nq","xauusd","futures","ict","signal","entry","stop","target","session","long","short","sweep","fvg","liquidity","msb","mss")),
    ("keeplodge", ("keeplodge","villa","airbnb","booking","property","host","str","rental","serenity")),
    ("probuild",  ("probuild","trades contractor","plumber","roofer","electrician","hvac")),
    ("nova",      ("nova algo","discord","webhook","assistant","briefing","agent","debrief")),
    ("ideas",     ("idea","launch","product","startup","saas","build")),
    ("personal",  (" i ","my ","me ","personal","feel","today","yesterday","mindset","energy")),
]


def classify(text: str) -> str:
    t = (text or "").lower()
    for cat, keywords in _CAT_RULES:
        if any(k in t for k in keywords):
            return cat
    return "general"


def extract_tags(text: str) -> list[str]:
    words = re.findall(r"\b[A-Za-z]{4,}\b", text or "")
    stop = {
        "that","this","with","from","have","will","what","when","your","they",
        "been","were","about","into","after","where","these","those","than",
        "just","like","only","over","some","then","also","such","them","here",
        "even","does","done","need","want","make","used","then",
    }
    uniq = []
    seen = set()
    for w in words:
        lw = w.lower()
        if lw in stop or lw in seen:
            continue
        seen.add(lw)
        uniq.append(lw)
        if len(uniq) >= 5:
            break
    return uniq
