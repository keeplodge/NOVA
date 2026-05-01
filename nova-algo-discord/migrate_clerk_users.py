"""
migrate_clerk_users.py — copy Clerk Development users + publicMetadata to Production.

What it does:
  1. Reads Dev sk_test_* from C:\\Users\\User\\nova-algo-site\\.env.production.local
     (this is the env file you pulled with `vercel env pull` earlier)
  2. Reads Prod sk_live_* from a one-time file at C:\\Users\\User\\nova\\.tmp_clerk_prod.env
     (you create this — see usage below)
  3. Enumerates every user in Dev via the Clerk Backend API
  4. For each Dev user: creates the same user in Prod with:
       - same email addresses (verified=true)
       - same first_name / last_name / username
       - same publicMetadata (fills, tradersPostWebhooks, discordUserId, isFounder,
         isCoFounder, tier, isBeta, audit ring, everything)
       - same privateMetadata
  5. Skips creation (and just updates metadata) if a user with the same email
     already exists in Prod
  6. Logs every action to migrate_clerk_users.log

What it does NOT do:
  - Passwords don't transfer (Clerk policy). Cohort members will need to do
    "Forgot password" on first sign-in to Prod.
  - Linked OAuth accounts (Discord OAuth identities) don't transfer.

USAGE:

  1. Save the prod sk_live_ to a file (single line, no quotes):
       C:\\Users\\User\\nova\\.tmp_clerk_prod.env
       Contents: CLERK_SECRET_KEY_PROD=sk_live_<your_value>

  2. Run:
       python C:\\Users\\User\\nova\\nova-algo-discord\\migrate_clerk_users.py

  3. After successful migration, DELETE the .tmp file so the secret isn't on disk:
       Remove-Item C:\\Users\\User\\nova\\.tmp_clerk_prod.env
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

DEV_ENV_PATH = r"C:\Users\User\nova-algo-site\.env.production.local"
PROD_KEY_PATH = r"C:\Users\User\nova\.tmp_clerk_prod.env"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrate_clerk_users.log")

CLERK_API = "https://api.clerk.com/v1"


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_env_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=\s*"?([^"]*)"?$', line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def clerk_request(method: str, path: str, secret: str, body: dict | None = None,
                  params: dict | None = None) -> tuple[int, dict | str]:
    url = f"{CLERK_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {secret}")
    req.add_header("User-Agent", "nova-algo-clerk-migrate/1.0 (admin)")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode()
        try:
            return e.code, json.loads(body_resp)
        except Exception:
            return e.code, body_resp


def fetch_all_dev_users(dev_key: str) -> list[dict]:
    users = []
    offset = 0
    page_size = 100
    while True:
        status, body = clerk_request("GET", "/users", dev_key, params={"limit": page_size, "offset": offset})
        if status != 200:
            log(f"FATAL fetching Dev users at offset {offset}: HTTP {status} :: {body}")
            sys.exit(1)
        if not isinstance(body, list):
            log(f"unexpected Dev /users response shape: {body}")
            sys.exit(1)
        users.extend(body)
        if len(body) < page_size:
            break
        offset += page_size
    return users


def find_prod_user_by_email(prod_key: str, email: str) -> dict | None:
    if not email:
        return None
    status, body = clerk_request("GET", "/users", prod_key,
                                 params={"email_address": email})
    if status == 200 and isinstance(body, list) and body:
        return body[0]
    return None


def primary_email(user: dict) -> str:
    primary_id = user.get("primary_email_address_id")
    for ea in (user.get("email_addresses") or []):
        if ea.get("id") == primary_id:
            return ea.get("email_address", "")
    eas = user.get("email_addresses") or []
    return eas[0].get("email_address", "") if eas else ""


def all_emails(user: dict) -> list[str]:
    return [ea.get("email_address") for ea in (user.get("email_addresses") or []) if ea.get("email_address")]


def create_prod_user(prod_key: str, dev_user: dict) -> tuple[int, dict | str]:
    payload = {
        "email_address": all_emails(dev_user),
        "skip_password_requirement": True,
        "skip_password_checks": True,
        "public_metadata": dev_user.get("public_metadata", {}) or {},
        "private_metadata": dev_user.get("private_metadata", {}) or {},
        "unsafe_metadata": dev_user.get("unsafe_metadata", {}) or {},
    }
    if dev_user.get("first_name"): payload["first_name"] = dev_user["first_name"]
    if dev_user.get("last_name"):  payload["last_name"]  = dev_user["last_name"]
    if dev_user.get("username"):   payload["username"]   = dev_user["username"]
    return clerk_request("POST", "/users", prod_key, body=payload)


def update_prod_metadata(prod_key: str, prod_user_id: str, dev_user: dict) -> tuple[int, dict | str]:
    payload = {
        "public_metadata": dev_user.get("public_metadata", {}) or {},
        "private_metadata": dev_user.get("private_metadata", {}) or {},
    }
    return clerk_request("PATCH", f"/users/{prod_user_id}/metadata", prod_key, body=payload)


def main() -> None:
    log("=" * 60)
    log("Clerk Dev -> Prod migration starting")
    log("=" * 60)

    # Dev key may be in either the Vercel-pulled env file OR the tmp prod-keys
    # file (we accept both — Vercel sometimes strips secret values during env pull).
    dev_env = read_env_file(DEV_ENV_PATH)
    dev_key = dev_env.get("CLERK_SECRET_KEY", "")
    if not dev_key.startswith("sk_test_"):
        # Fallback: look for CLERK_SECRET_KEY_DEV in the tmp file
        prod_env_check = read_env_file(PROD_KEY_PATH)
        dev_key = prod_env_check.get("CLERK_SECRET_KEY_DEV", "")
    if not dev_key.startswith("sk_test_"):
        log(f"FATAL: Dev sk_test_ key not found in either:")
        log(f"   {DEV_ENV_PATH}    (key: CLERK_SECRET_KEY)")
        log(f"   {PROD_KEY_PATH}    (key: CLERK_SECRET_KEY_DEV)")
        sys.exit(1)
    log(f"Dev key OK ({dev_key[:14]}...)")

    prod_env = read_env_file(PROD_KEY_PATH)
    prod_key = prod_env.get("CLERK_SECRET_KEY_PROD", "") or prod_env.get("CLERK_SECRET_KEY", "")
    if not prod_key.startswith("sk_live_"):
        log(f"FATAL: Prod key not found or not sk_live_ prefixed in {PROD_KEY_PATH}")
        log(f"       Create that file with one line:")
        log(f"         CLERK_SECRET_KEY_PROD=sk_live_<your_value>")
        sys.exit(1)
    log(f"Prod key OK ({prod_key[:14]}...)")

    dev_users = fetch_all_dev_users(dev_key)
    log(f"Fetched {len(dev_users)} Dev users")

    created = 0
    updated = 0
    skipped = 0
    failed = 0
    for u in dev_users:
        email = primary_email(u)
        name = (u.get("first_name") or "") + " " + (u.get("last_name") or "")
        meta = u.get("public_metadata") or {}
        flags = []
        if meta.get("isFounder"): flags.append("FOUNDER")
        if meta.get("isCoFounder"): flags.append("CO-FOUNDER")
        if meta.get("isBeta"): flags.append("BETA")
        if meta.get("tier"): flags.append(meta["tier"])
        fills_count = len(meta.get("fills") or [])
        webhooks_count = len(meta.get("tradersPostWebhooks") or [])
        log(f"  > {email:40s} {name.strip():18s} fills={fills_count:3d} hooks={webhooks_count} {' '.join(flags)}")

        # Skip if already in prod
        existing = find_prod_user_by_email(prod_key, email)
        if existing:
            log(f"      already in Prod (id={existing.get('id','?')}), patching metadata...")
            status, body = update_prod_metadata(prod_key, existing["id"], u)
            if status in (200, 201):
                updated += 1
                log(f"      [updated]")
            else:
                failed += 1
                log(f"      [update-fail] HTTP {status}: {str(body)[:200]}")
            continue

        # Create
        status, body = create_prod_user(prod_key, u)
        if status in (200, 201) and isinstance(body, dict) and body.get("id"):
            created += 1
            log(f"      [created] new prod_id={body['id']}")
        elif status == 422 and "already exists" in str(body).lower():
            skipped += 1
            log(f"      [skip] email collision (already in Prod): {body}")
        else:
            failed += 1
            log(f"      [create-fail] HTTP {status}: {str(body)[:300]}")
        time.sleep(0.3)  # gentle on Clerk's rate limit

    log("=" * 60)
    log(f"DONE  created={created}  updated={updated}  skipped={skipped}  failed={failed}")
    log(f"      total Dev users: {len(dev_users)}")
    log(f"      log file: {LOG_PATH}")
    log("=" * 60)
    log("NEXT STEPS:")
    log("  1. Visit https://novaalgo.org/sign-in and try signing in as yourself.")
    log("     If it says 'no account' but you see your email after entering it,")
    log("     click 'Forgot password' and reset — passwords don't transfer between")
    log("     Clerk Dev and Prod instances.")
    log("  2. Once you can sign in, /portal should show your dashboard with all fills.")
    log("  3. DELETE the prod key file: Remove-Item C:\\Users\\User\\nova\\.tmp_clerk_prod.env")


if __name__ == "__main__":
    main()
