"""
test_traderspost_bracket.py — guarantees every code path that touches
TradersPost translates flat Pine `sl`/`tp` to nested `stopLoss.stopPrice`
+ `takeProfit.limitPrice`. Run pre-deploy.

Why this exists: 2026-04-30 + 2026-05-01 had two consecutive prod bugs
where a code path touching TradersPost dropped the bracket — first Pine
itself (no sl/tp emitted), then subscriber_fanout (forwarded raw flat).
Both bugs cost cohort risk exposure. This test catches both classes.

Run:
  python tests/test_traderspost_bracket.py
Exit 0 if all paths emit nested brackets. Exit 1 + diff on failure.
"""
from __future__ import annotations

import json
import os
import sys

# Make sibling modules importable when run from repo root or tests/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Sample Pine alert payload — what nova_master_v14.pine v1.4.1 emits on entry.
SAMPLE_PINE_LONG = {
    "ticker":  "NQ1!",
    "action":  "buy",
    "price":   21500.00,
    "sl":      21475.00,        # $500 risk on NQ E-mini ($20/pt × 25pts)
    "tp":      21600.00,        # $2000 target ($20/pt × 100pts)
    "qty":     1,
    "comment": "NY_AM_ORB_long",
    "secret":  "test-secret",
}
SAMPLE_PINE_SHORT = {
    **SAMPLE_PINE_LONG,
    "action":  "sell",
    "price":   21500.00,
    "sl":      21525.00,
    "tp":      21400.00,
    "comment": "NY_AM_ORB_short",
}


def assert_bracket_attached(payload: dict, path_label: str, source_pine: dict) -> list[str]:
    """Return list of failure strings (empty list = pass)."""
    fails: list[str] = []
    sl_expected = source_pine.get("sl")
    tp_expected = source_pine.get("tp")

    # stopLoss must be nested
    sl_obj = payload.get("stopLoss")
    if not isinstance(sl_obj, dict):
        fails.append(f"  ✗ {path_label}: stopLoss missing or not nested object")
    else:
        if "stopPrice" not in sl_obj:
            fails.append(f"  ✗ {path_label}: stopLoss missing stopPrice key")
        elif float(sl_obj["stopPrice"]) != float(sl_expected):
            fails.append(f"  ✗ {path_label}: stopLoss.stopPrice = {sl_obj['stopPrice']}, expected {sl_expected}")

    # takeProfit must be nested
    tp_obj = payload.get("takeProfit")
    if not isinstance(tp_obj, dict):
        fails.append(f"  ✗ {path_label}: takeProfit missing or not nested object")
    else:
        if "limitPrice" not in tp_obj:
            fails.append(f"  ✗ {path_label}: takeProfit missing limitPrice key")
        elif float(tp_obj["limitPrice"]) != float(tp_expected):
            fails.append(f"  ✗ {path_label}: takeProfit.limitPrice = {tp_obj['limitPrice']}, expected {tp_expected}")

    # Defensive: ensure no flat sl/tp leaked through
    if "sl" in payload and not isinstance(payload.get("sl"), dict):
        fails.append(f"  ✗ {path_label}: flat 'sl' field leaked through to TradersPost shape (TradersPost ignores this)")
    if "tp" in payload and not isinstance(payload.get("tp"), dict):
        fails.append(f"  ✗ {path_label}: flat 'tp' field leaked through to TradersPost shape (TradersPost ignores this)")

    return fails


def test_founder_primary_route(pine: dict, label: str) -> list[str]:
    """The founder's primary TP route runs through app.build_traderspost_payload."""
    from app import build_traderspost_payload  # type: ignore
    out = build_traderspost_payload(pine, session="NY_AM")
    return assert_bracket_attached(out, f"founder-primary({label})", pine)


def test_subscriber_fanout(pine: dict, label: str) -> list[str]:
    """Subscriber fanout uses _to_traderspost_shape before fanning."""
    from subscriber_fanout import _to_traderspost_shape  # type: ignore
    out = _to_traderspost_shape(pine)
    return assert_bracket_attached(out, f"subscriber-fanout({label})", pine)


def test_pine_payload_has_required_fields() -> list[str]:
    """Lint: Pine source must emit all required fields in entry alert payload."""
    fails: list[str] = []
    pine_path = os.path.join(ROOT, "nova_master_v14.pine")
    if not os.path.exists(pine_path):
        return [f"  ⚠ pine source file not found: {pine_path}"]
    with open(pine_path, "r", encoding="utf-8") as f:
        pine_src = f.read()
    # Check both long and short payload constructions emit sl + tp
    for tag, build_var in [("long", "long_payload"), ("short", "short_payload")]:
        # Find the line constructing the payload
        marker_idx = pine_src.find(f"{build_var} = ")
        if marker_idx < 0:
            fails.append(f"  ✗ pine-source({tag}): {build_var} = ... line not found")
            continue
        # Take the next ~600 chars (one line) and check for sl + tp tokens
        snippet = pine_src[marker_idx:marker_idx + 600]
        end = snippet.find("\n")
        line = snippet[:end] if end > 0 else snippet
        for required in ('"sl":', '"tp":', '"action":', '"price":', '"qty":', '"comment":', '"secret":'):
            if required not in line:
                fails.append(f"  ✗ pine-source({tag}): {build_var} missing {required} field")
    return fails


def main() -> int:
    print("=" * 60)
    print("TradersPost bracket parity test")
    print("=" * 60)
    all_fails: list[str] = []

    # 1. Pine source emits all required fields
    print("\n[1] Pine source field lint")
    pine_fails = test_pine_payload_has_required_fields()
    all_fails.extend(pine_fails)
    if not pine_fails:
        print("  ✓ nova_master_v14.pine emits sl+tp+action+price+qty+comment+secret on entry alerts")

    # 2. Founder primary route — long
    print("\n[2] Founder primary route via app.build_traderspost_payload")
    f_long = test_founder_primary_route(SAMPLE_PINE_LONG, "long")
    f_short = test_founder_primary_route(SAMPLE_PINE_SHORT, "short")
    all_fails.extend(f_long); all_fails.extend(f_short)
    if not f_long and not f_short:
        print("  ✓ both long and short produce nested stopLoss + takeProfit")

    # 3. Subscriber fanout — uses translator
    print("\n[3] Subscriber fanout via _to_traderspost_shape")
    s_long = test_subscriber_fanout(SAMPLE_PINE_LONG, "long")
    s_short = test_subscriber_fanout(SAMPLE_PINE_SHORT, "short")
    all_fails.extend(s_long); all_fails.extend(s_short)
    if not s_long and not s_short:
        print("  ✓ both long and short produce nested stopLoss + takeProfit")

    # 4. Cross-path consistency: founder primary and fanout must produce equivalent brackets
    print("\n[4] Cross-path consistency (founder primary vs fanout)")
    from app import build_traderspost_payload  # type: ignore
    from subscriber_fanout import _to_traderspost_shape  # type: ignore
    primary = build_traderspost_payload(SAMPLE_PINE_LONG, session="NY_AM")
    fanout  = _to_traderspost_shape(SAMPLE_PINE_LONG)
    cross_fails: list[str] = []
    for k in ("ticker", "action", "stopLoss", "takeProfit"):
        p_val = primary.get(k); f_val = fanout.get(k)
        if isinstance(p_val, dict) and isinstance(f_val, dict):
            # nested: compare key children
            for sk, sv in p_val.items():
                if f_val.get(sk) != sv:
                    cross_fails.append(f"  ✗ cross-path: {k}.{sk} mismatch — primary={sv} fanout={f_val.get(sk)}")
        elif p_val != f_val:
            cross_fails.append(f"  ✗ cross-path: {k} mismatch — primary={p_val} fanout={f_val}")
    all_fails.extend(cross_fails)
    if not cross_fails:
        print("  ✓ primary and fanout produce equivalent stopLoss/takeProfit/action/ticker for the same Pine input")

    print()
    print("=" * 60)
    if all_fails:
        print(f"❌ {len(all_fails)} bracket/field validation failure(s):")
        for f in all_fails:
            print(f)
        print()
        print("Do NOT deploy — a TradersPost code path is dropping the bracket.")
        return 1
    print("✅ All TradersPost bracket paths validated. Safe to deploy.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
