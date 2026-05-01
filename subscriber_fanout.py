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
HALT_URL = os.environ.get("NOVA_HALT_URL", "")  # e.g. https://novaalgo.org/api/fleet/halt
FILLS_URL = os.environ.get("NOVA_FILLS_URL", "") # e.g. https://novaalgo.org/api/fills/record

# Founder Clerk userId — when set, each NOVA signal also writes per-eval
# fill entries to founder's Clerk metadata so /portal/journal tap-buttons
# work for the founder's own accounts.
FOUNDER_USER_ID = os.environ.get("NOVA_FOUNDER_USER_ID", "")

_cache: dict[str, Any] = {"at": 0.0, "data": []}
_halt_cache: dict[str, Any] = {"at": 0.0, "halted": False, "reason": None}
_CACHE_TTL_SECONDS = 30
_HALT_TTL_SECONDS = 15  # check halt more frequently than the sub list
_FANOUT_DEADLINE = 8.0


def _halted() -> tuple[bool, str | None]:
    """Cached check of the founder's fleet kill switch."""
    if not HALT_URL or not FANOUT_SECRET:
        return False, None
    now = time.time()
    if now - _halt_cache["at"] < _HALT_TTL_SECONDS:
        return _halt_cache["halted"], _halt_cache.get("reason")
    try:
        req = urllib.request.Request(
            HALT_URL,
            headers={"X-NOVA-Fanout-Secret": FANOUT_SECRET},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = json.loads(r.read().decode())
        halted = bool(payload.get("halted"))
        reason = payload.get("haltReason")
        _halt_cache.update({"at": now, "halted": halted, "reason": reason})
        return halted, reason
    except Exception as e:  # noqa: BLE001
        print(f"[fanout] halt check failed: {e}")
        return _halt_cache.get("halted", False), _halt_cache.get("reason")


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


def _to_traderspost_shape(payload: dict, size_multiplier: float = 1.0) -> dict:
    """Translate Pine's flat alert payload to TradersPost's nested order shape.

    Pine emits {action,price,sl,tp,qty,...}. TradersPost requires nested
    `stopLoss.stopPrice` and `takeProfit.limitPrice` to attach a bracket on
    entry — without these, subscriber accounts open a NAKED position even
    though the founder's primary route gets a bracket via app.build_traderspost_payload.

    `size_multiplier` scales the qty for per-account sizing (0.5x / 1x / 2x).
    Default 1.0 preserves original Pine qty. Always rounds up to at least 1
    contract — fractional rounds to ceiling so 0.5x of qty=1 still trades 1 contract.
    """
    action = (payload.get("action") or "").lower()
    sentiment_map = {"buy": "bullish", "sell": "bearish"}
    base_qty = int(payload.get("qty") or payload.get("quantity") or 1)
    # Apply multiplier — clamp final qty between 1 and 20 (sanity bound)
    import math
    scaled_qty = max(1, min(20, math.ceil(base_qty * size_multiplier)))
    out: dict[str, Any] = {
        "ticker":    str(payload.get("ticker", "")).upper().strip(),
        "action":    action,
        "price":     float(payload.get("price", 0)) if payload.get("price") is not None else None,
        "quantity":  scaled_qty,
        "orderType": payload.get("orderType", "market"),
        "sentiment": sentiment_map.get(action, "bullish"),
        "comment":   payload.get("comment", ""),
    }
    sl = payload.get("sl")
    tp = payload.get("tp")
    if sl is not None:
        try: out["stopLoss"]   = {"type": "stop", "stopPrice": float(sl)}
        except (TypeError, ValueError): pass
    if tp is not None:
        try: out["takeProfit"] = {"limitPrice": float(tp)}
        except (TypeError, ValueError): pass
    return out


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

    Returns a small result dict for logging. Never raises. Skips fanout entirely
    if the founder has flipped the global halt switch on novaalgo.org.
    """
    halted, reason = _halted()
    if halted:
        print(f"[fanout] HALTED — global kill switch active ({reason or 'manual'}); skipping fanout")
        return {"fanned_to": 0, "ok": 0, "fail": 0, "halted": True, "reason": reason, "details": []}

    subs = _fetch_subscribers()
    if not subs:
        return {"fanned_to": 0, "ok": 0, "fail": 0, "details": []}

    results: list[dict] = []
    threads: list[threading.Thread] = []
    lock = threading.Lock()

    # Translate flat Pine payload (sl/tp) to TradersPost nested shape
    # (stopLoss/takeProfit) PER subscriber so each gets their personalized
    # sizeMultiplier applied to qty. Bug 2026-05-01: fanning the raw Pine
    # body left subs naked while founder's primary route had a proper bracket.

    def _worker(sub: dict) -> None:
        url = sub.get("webhookUrl")
        if not url:
            return
        # Per-subscriber size scaling — defaults to 1x. Set via /portal/connect.
        mul_raw = sub.get("sizeMultiplier")
        size_multiplier = 1.0
        try:
            if mul_raw is not None:
                size_multiplier = float(mul_raw)
                if not (0.25 <= size_multiplier <= 4.0):
                    size_multiplier = 1.0
        except (TypeError, ValueError):
            size_multiplier = 1.0
        tp_payload = _to_traderspost_shape(payload, size_multiplier=size_multiplier)
        status, body = _post_one(url, tp_payload)
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

    # Log successful fills back to novaalgo.org for the subscriber's journal.
    # Also: when NOVA_FOUNDER_USER_ID is set, writes one entry per active
    # eval account on the founder's record so /portal/journal tap-buttons
    # work for the founder's own real accounts (Apex 100K, Lucid 50K, etc.).
    if FILLS_URL and FANOUT_SECRET:
        try:
            entries = []
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            # Subscriber entries (one per delivered fanout)
            for r in results:
                if not r.get("ok"):
                    continue
                matching_sub = next(
                    (s for s in subs if s.get("userId") == r.get("userId")),
                    None,
                )
                entries.append({
                    "userId": r.get("userId"),
                    "action": payload.get("action", "unknown"),
                    "price": payload.get("price"),
                    "sl": payload.get("sl"),
                    "tp": payload.get("tp"),
                    "ts": now_iso,
                    "label": matching_sub.get("accountLabel") if matching_sub else None,
                    "outcome": "filled",
                })

            # Founder entries — one per eval account so per-account PnL
            # tracking works across all of Sir's prop accounts.
            if FOUNDER_USER_ID:
                try:
                    # Pull the live eval roster from app.py — same dict that
                    # /status uses, so it always reflects what's actually live.
                    from app import EVAL_ACCOUNTS  # local import to avoid bootup circularity
                    for acct_id, acct in EVAL_ACCOUNTS.items():
                        entries.append({
                            "userId": FOUNDER_USER_ID,
                            "action": payload.get("action", "unknown"),
                            "price": payload.get("price"),
                            "sl": payload.get("sl"),
                            "tp": payload.get("tp"),
                            "ts": now_iso,
                            "label": acct.get("label", acct_id),
                            "outcome": "filled",
                        })
                except Exception as fe:  # noqa: BLE001
                    print(f"[fanout] founder eval-roster import failed: {fe}")

            if entries:
                req = urllib.request.Request(
                    FILLS_URL,
                    data=json.dumps({"entries": entries}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-NOVA-Fanout-Secret": FANOUT_SECRET,
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:  # noqa: BLE001
            print(f"[fanout] fills journal log failed: {e}")

    return {
        "fanned_to": len(subs),
        "ok": ok_count,
        "fail": len(subs) - ok_count,
        "details": results,
    }


def fanout_exit(data: dict) -> dict:
    """Send an exit signal to every approved subscriber's TradersPost webhook.

    Mirrors `fanout_signal` but with an exit-specific payload (no entry price,
    no SL/TP — just `action: "exit"`). Used by the Pine Master strategy when:
      - active SL is touched (original SL / BE / Trail level)
      - session close at 11:00 ET with position still open

    Returns the same shape as `fanout_signal` for consistent logging.
    """
    halted, reason = _halted()
    if halted:
        print(f"[fanout-exit] HALTED — global kill switch active ({reason or 'manual'})")
        return {"fanned_to": 0, "ok": 0, "fail": 0, "halted": True, "reason": reason, "details": []}

    subs = _fetch_subscribers()
    if not subs:
        return {"fanned_to": 0, "ok": 0, "fail": 0, "details": []}

    payload = {
        "ticker":  str(data.get("ticker", "")).upper().strip(),
        "action":  "exit",
        "comment": str(data.get("comment", "NOVA exit")),
    }

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
                "email":  sub.get("email"),
                "label":  sub.get("accountLabel"),
                "ok":     ok,
                "status": status,
                "body":   body,
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
    print(f"[fanout-exit] {ok_count}/{len(subs)} subscribers received exit")

    return {
        "fanned_to": len(subs),
        "ok":        ok_count,
        "fail":      len(subs) - ok_count,
        "details":   results,
    }
