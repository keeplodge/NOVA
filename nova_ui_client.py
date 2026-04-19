"""
NOVA UI Client — thin Python helper to push state + log entries to the
nova_ui_server (the browser dashboard). Used by nova_assistant.py and
nova_local.py so voice/trade/market events appear live in the browser UI.

Every function is fire-and-forget — silently swallows any failure so the
main assistant loop is never blocked by UI trouble.
"""
from __future__ import annotations

import os
import time
from typing import Any

try:
    import httpx
    _HTTPX_AVAILABLE = True
except Exception:
    _HTTPX_AVAILABLE = False


UI_URL = os.environ.get("NOVA_UI_URL", "http://127.0.0.1:7336")


def _post(payload: dict) -> None:
    if not _HTTPX_AVAILABLE:
        return
    try:
        httpx.post(f"{UI_URL}/push", json=payload, timeout=1.5)
    except Exception:
        pass


def push_state(state: dict[str, Any]) -> None:
    """Merge a partial state patch and broadcast to all connected browsers."""
    _post({"type": "state", "payload": state})


def push_mode(mode: str, color: str | None = None, status_text: str | None = None) -> None:
    """Update the assistant mode (idle / listening / speaking / alert / trade)."""
    payload: dict[str, Any] = {"mode": mode}
    if color is not None:         payload["color"]       = color
    if status_text is not None:   payload["status_text"] = status_text
    _post({"type": "mode", "payload": payload})


def push_log(kind: str, msg: str, time_str: str | None = None) -> None:
    """Append a single log entry for the dashboard's command stream."""
    _post({"type": "log", "payload": {
        "time": time_str or time.strftime("%H:%M:%S"),
        "kind": kind,
        "msg":  msg,
    }})


def is_online() -> bool:
    if not _HTTPX_AVAILABLE:
        return False
    try:
        r = httpx.get(f"{UI_URL}/health", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False
