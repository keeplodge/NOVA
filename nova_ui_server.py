"""
NOVA UI Sidecar Server — serves the browser-rendered assistant dashboard.

Runs as a FastAPI process at http://127.0.0.1:7336 (NOVA_UI_PORT env to
override). Serves nova_ui/ as static files and exposes a WebSocket at /ws
that broadcasts assistant state to every connected tab.

The NOVA Assistant (nova_assistant.py / nova_local.py) talks to this
server via HTTP POST to /push so it doesn't need to own a WebSocket hub
itself. One producer -> many subscribers pattern.

Usage:
    python nova_ui_server.py
    # then open http://localhost:7336/ in your browser

Or run via nova_launch_ui.py which boots the server + opens the tab +
starts nova_local in parallel.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


# ── Config ─────────────────────────────────────────────────────────────────────

HOST      = os.environ.get("NOVA_UI_HOST", "127.0.0.1")
PORT      = int(os.environ.get("NOVA_UI_PORT", "7336"))
ROOT      = Path(__file__).parent / "nova_ui"


# ── FastAPI + state cache ─────────────────────────────────────────────────────

app = FastAPI(title="NOVA Assistant UI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Serve static files under /static so the HTML can reference /static/styles.css
app.mount("/static", StaticFiles(directory=str(ROOT)), name="static")

# Global state cache — new WebSocket clients get this immediately on connect
_state: dict[str, Any] = {
    "mode":           "idle",
    "color":          "#00e5ff",
    "session":        "—",
    "nq":             "—",
    "vix":            "—",
    "daily_loss":     "$0",
    "remaining":      "$500",
    "trades_today":   "0",
    "last_signal":    "—",
    "brain_status":   "CHECKING",
    "brain_memories": "—",
    "next_session":   "—",
    "next_session_sub": "",
    "status_text":    "IDLE",
    "neuro":          {"trading": 0.4, "ideas": 0.3, "nova": 0.5, "personal": 0.2},
}

_log: list[dict] = []
_max_log = 400

_active_connections: set[WebSocket] = set()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    html_path = ROOT / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>NOVA UI</h1><p>nova_ui/index.html not found</p>", status_code=500)
    return FileResponse(html_path)


@app.get("/health")
async def health():
    return {"status": "online", "subscribers": len(_active_connections), "log_entries": len(_log)}


@app.get("/state")
async def state():
    return _state


@app.post("/push")
async def push(body: dict):
    """
    State push endpoint. Accepts a partial payload:

    { "type": "state"|"mode"|"log", "payload": {...} }

    - state  : shallow-merge payload into the global cache, broadcast snapshot
    - mode   : just updates mode + optional color
    - log    : append a log entry  {time, kind, msg}
    """
    kind = body.get("type", "state")
    payload = body.get("payload", {}) or {}

    if kind == "state":
        _state.update(payload)
        await broadcast({"type": "state", "payload": _state})
    elif kind == "mode":
        _state["mode"] = payload.get("mode", _state["mode"])
        if "color" in payload:
            _state["color"] = payload["color"]
        if "status_text" in payload:
            _state["status_text"] = payload["status_text"]
        await broadcast({"type": "state", "payload": _state})
    elif kind == "log":
        entry = {
            "time": payload.get("time") or time.strftime("%H:%M:%S"),
            "kind": payload.get("kind") or "state",
            "msg":  payload.get("msg")  or "",
        }
        _log.insert(0, entry)
        if len(_log) > _max_log:
            _log.pop()
        await broadcast({"type": "log", "payload": entry})
    else:
        return JSONResponse({"ok": False, "reason": f"unknown type {kind!r}"}, status_code=400)

    return {"ok": True}


@app.post("/chat")
async def chat(body: dict):
    """
    Conversational endpoint. Browser POSTs {"text": "..."} and we route it
    through nova_command_ai.classify_and_respond (Claude → Ollama → keyword
    fallback). Returns {"reply": "...", "action": "...", "reasoning": "..."}.
    Also broadcasts both sides of the exchange to all connected WS clients
    so every open tab sees the same conversation.
    """
    text = (body or {}).get("text", "").strip()
    if not text:
        return JSONResponse({"ok": False, "reason": "empty text"}, status_code=400)

    # Broadcast user message first so every tab echoes it
    user_entry = {
        "time": time.strftime("%H:%M:%S"),
        "kind": "user",
        "msg":  text,
    }
    _log.insert(0, user_entry)
    if len(_log) > _max_log:
        _log.pop()
    await broadcast({"type": "log", "payload": user_entry})

    # Route through the command AI (runs sync — push to thread so we don't
    # block the event loop while Ollama thinks)
    try:
        from nova_command_ai import classify_and_respond, handle_remember, handle_recall
    except Exception as e:
        reply = f"Command AI unavailable: {e}"
        action, reasoning = "CHAT", "import error"
    else:
        try:
            cr = await asyncio.to_thread(classify_and_respond, text)
            reply     = cr.spoken or "Done, Sir."
            action    = cr.action
            reasoning = cr.reasoning

            # Side effects for REMEMBER / RECALL
            if action == "REMEMBER" and cr.payload:
                ok = handle_remember(cr.payload)
                if not ok:
                    reply += " (couldn't persist, though.)"
            elif action == "RECALL" and cr.payload:
                recalled = handle_recall(cr.payload, limit=3)
                if recalled:
                    reply = f"{reply}\n\n{recalled}"
        except Exception as e:
            reply, action, reasoning = f"Chat error: {e}", "CHAT", "exception"

    # Broadcast the assistant reply
    nova_entry = {
        "time": time.strftime("%H:%M:%S"),
        "kind": "nova",
        "msg":  reply,
    }
    _log.insert(0, nova_entry)
    if len(_log) > _max_log:
        _log.pop()
    await broadcast({"type": "log", "payload": nova_entry})

    # Flash mode to 'speaking' briefly so the orb + wave respond visually
    _state["mode"] = "speaking"
    await broadcast({"type": "state", "payload": _state})
    asyncio.get_event_loop().call_later(2.5, lambda: _reset_mode())

    return {"ok": True, "reply": reply, "action": action, "reasoning": reasoning}


def _reset_mode():
    _state["mode"] = "idle"
    try:
        asyncio.get_event_loop().create_task(
            broadcast({"type": "state", "payload": _state})
        )
    except Exception:
        pass


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _active_connections.add(ws)

    # Send the current snapshot immediately
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "payload": _state}))
        # Replay the last 30 log entries so the new tab isn't blank
        for entry in reversed(_log[:30]):
            await ws.send_text(json.dumps({"type": "log", "payload": entry}))
    except Exception:
        pass

    try:
        while True:
            # We don't accept messages from clients, but keep the connection open.
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _active_connections.discard(ws)


# ── Built-in data poller — keeps the dashboard live even if nova_local
#    isn't running. Polls every POLL_INTERVAL seconds and broadcasts a state
#    patch to every connected browser.
# ──────────────────────────────────────────────────────────────────────────────

BRAIN_URL        = os.environ.get("NOVA_BRAIN_URL", "http://127.0.0.1:7337")
NOVA_STATUS_URL  = os.environ.get("NOVA_SERVER_URL", "https://nova-production-72f5.up.railway.app") + "/status"
POLL_INTERVAL    = int(os.environ.get("NOVA_UI_POLL_INTERVAL", "30"))


async def _poll_brain() -> dict:
    """Returns {brain_status, brain_memories}."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{BRAIN_URL}/health")
            if r.status_code != 200:
                return {"brain_status": "OFFLINE", "brain_memories": "—"}
            # memory count
            rc = await c.get(f"{BRAIN_URL}/recent", params={"limit": 10000})
            mem_count = len(rc.json()) if rc.status_code == 200 else "—"
            return {"brain_status": "ONLINE", "brain_memories": str(mem_count)}
    except Exception:
        return {"brain_status": "OFFLINE", "brain_memories": "—"}


async def _poll_nova_server() -> dict:
    """Returns {session, daily_loss, remaining, trades_today, last_signal}."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(NOVA_STATUS_URL)
            if r.status_code != 200:
                return {}
            d = r.json()
            loss      = d.get("daily_loss", 0.0) or 0.0
            remaining = d.get("loss_remaining", 500.0) or 500.0
            session   = d.get("active_session") or "None"
            trades    = d.get("trades_today", 0)
            ls        = d.get("last_signal") or {}
            last_str  = (f"{ls.get('action', '?').upper()} {ls.get('ticker', '')} "
                         f"@ {ls.get('price', '?')} · {ls.get('grade', '')}").strip()
            return {
                "session":      session if session else "None",
                "daily_loss":   f"${loss:.0f}",
                "remaining":    f"${remaining:.0f}",
                "trades_today": str(trades),
                "last_signal":  last_str if ls else "—",
            }
    except Exception:
        return {}


async def _poll_market_data() -> dict:
    """Returns {vix, nq}. Uses yfinance — latency tolerant."""
    try:
        import yfinance as yf
        out = {}
        try:
            v = yf.Ticker("^VIX").history(period="1d", interval="5m")
            if v is not None and not v.empty:
                out["vix"] = f"{float(v['Close'].iloc[-1]):.1f}"
        except Exception:
            pass
        try:
            n = yf.Ticker("NQ=F").history(period="1d", interval="5m")
            if n is not None and not n.empty:
                out["nq"] = f"{float(n['Close'].iloc[-1]):,.0f}"
        except Exception:
            pass
        return out
    except Exception:
        return {}


def _compute_next_session() -> dict:
    """
    Returns {next_session, next_session_sub} for the sessions panel.
    Uses NOVA's own 3-session rhythm: Asia 19-23 EST, London 2-5, NY_AM 8:30-11.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    est = ZoneInfo("America/New_York")
    now = datetime.now(tz=est)
    sessions = [
        ("Asia",   (19, 0),  (23, 0)),
        ("London", (2,  0),  (5,  0)),
        ("NY AM",  (8, 30),  (11, 0)),
    ]
    current_min = now.hour * 60 + now.minute

    for name, start, end in sessions:
        s = start[0] * 60 + start[1]
        e = end[0]   * 60 + end[1]
        if s <= current_min < e:
            mins_left = e - current_min
            return {
                "next_session":     f"{name} LIVE",
                "next_session_sub": f"{mins_left//60}h {mins_left%60}m left",
            }

    # Next upcoming session (may be tomorrow)
    upcoming = []
    for name, start, end in sessions:
        s = start[0] * 60 + start[1]
        offset = s - current_min if s > current_min else s + 1440 - current_min
        upcoming.append((offset, name, start))
    upcoming.sort()
    offset, name, start = upcoming[0]
    return {
        "next_session":     f"{start[0]:02d}:{start[1]:02d} {name}",
        "next_session_sub": f"in {offset//60}h {offset%60}m",
    }


async def _poller_loop():
    """Runs forever — aggregates data from brain, Railway, yfinance; broadcasts."""
    # Initial warm-up delay so the server finishes booting
    await asyncio.sleep(0.8)
    while True:
        patch: dict[str, Any] = {}
        try:
            patch.update(await _poll_brain())
            patch.update(await _poll_nova_server())
            patch.update(await _poll_market_data())
            patch.update(_compute_next_session())
            # Light neuromodulator simulation — category-balance hint
            patch["neuro"] = {
                "trading":   0.55 if patch.get("session") not in ("None", "—") else 0.30,
                "ideas":     0.35,
                "nova":      0.60,
                "personal":  0.25,
            }
        except Exception as e:
            patch["status_text"] = f"POLLER ERROR"
            print(f"[nova_ui_server] poller error: {e}")

        if patch:
            _state.update(patch)
            await broadcast({"type": "state", "payload": _state})
        await asyncio.sleep(POLL_INTERVAL)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_poller_loop())


# ── Broadcast helper ─────────────────────────────────────────────────────────

async def broadcast(msg: dict):
    if not _active_connections:
        return
    raw = json.dumps(msg)
    dead = set()
    for ws in _active_connections:
        try:
            await ws.send_text(raw)
        except Exception:
            dead.add(ws)
    _active_connections.difference_update(dead)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"[nova_ui_server] starting on http://{HOST}:{PORT}")
    print(f"[nova_ui_server] serving static from {ROOT}")
    uvicorn.run(
        "nova_ui_server:app",
        host=HOST, port=PORT,
        reload=False, log_level="warning",
    )
