"""
subscriber_fanout.py — fan TV signals out to every NOVA Algo subscriber's
TradersPost webhook.

Calls the Vercel-hosted endpoint at NOVA_SUBSCRIBERS_URL with the shared
secret, gets the list of active subscribers (Clerk users who submitted a
TradersPost URL on /portal/connect), and POSTs the same signal payload to
each subscriber's webhook in parallel.

Runs AFTER the founder's own routing in app.py — never blocks the founder's
fills. Hard 8s deadline. Caches the subscriber list 30s.

Required env vars:
  NOVA_SUBSCRIBERS_URL   https://novaalgo.org/api/admin/subscribers/webhooks
  FANOUT_SHARED_SECRET   (same value as on Vercel)
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

SUBSCRIBERS_URL = os.environ.get("NOVA_SUBSCRIBERS_URL", "")
FANOUT_SECRET = os.environ.get("FANOUT_SHARED_SECRET", "")

_cache: dict[str, Any] = {"at": 0.0, "data": []}
_CACHE_TTL_SECONDS = 30
_FANOUT_DEADLINE = 8.0


def _fetch_subscribers() -> list[dict]:
    if not SUBSCRIBERS_URL or not FANOUT_SECRET:
        return []
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache["data"]
    try:
        req = urllib.request.Request(
            SUBSCRIBERS_URL,
            headers={"X-NOVA-Fanout-Secret": FANOUT_SECRET},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read().decode())
        subs = payload.get("subscribers", []) if payload.get("ok") else []
        _cache.update({"at": now, "data": subs})
        return subs
    except Exception as e:  # noqa: BLE001 — fanout must never raise
        print(f"[fanout] subscriber fetch failed: {e}")
        return _cache["data"]


def _post_one(url: str, payload: dict) -> tuple[int, str]:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def fanout_signal(payload: dict) -> dict:
    """Send `payload` to every active subscriber webhook in parallel.

    Returns a small result dict for logging. Never raises.
    """
    subs = _fetch_subscribers()
    if not subs:
        return {"fanned_to": 0, "ok": 0, "fail": 0, "details": []}

    results: list[dict] = []
    threads: list[threading.Thread] = []
    lock = threading.Lock()

    def _worker(sub: dict) -> None:
        url = sub.get("webhookUrl")
        if not url:
            return
        status, body = _post_one(url, payload)
        ok = 200 <= (status or 0) < 300
        with lock:
            results.append({
                "userId": sub.get("userId"),
                "email": sub.get("email"),
                "tier": sub.get("tier"),
                "label": sub.get("accountLabel"),
                "ok": ok,
                "status": status,
                "body": body,
            })

    for sub in subs:
        t = threading.Thread(target=_worker, args=(sub,), daemon=True)
        threads.append(t)
        t.start()

    deadline = time.time() + _FANOUT_DEADLINE
    for t in threads:
        remaining = max(0.0, deadline - time.time())
        t.join(timeout=remaining)

    ok_count = sum(1 for r in results if r["ok"])
    print(f"[fanout] {ok_count}/{len(subs)} subscribers received the signal")
    return {
        "fanned_to": len(subs),
        "ok": ok_count,
        "fail": len(subs) - ok_count,
        "details": results,
    }
