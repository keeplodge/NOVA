#!/usr/bin/env python3
"""
KeepLodge Waitlist Agent
Polls Netlify Forms for new host signups, sends welcome email via MailerLite,
tracks waitlist count, logs to Obsidian, and reports to NOVA morning briefing.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

NETLIFY_TOKEN    = os.getenv("NETLIFY_TOKEN")
NETLIFY_SITE_ID  = os.getenv("NETLIFY_SITE_ID")
NETLIFY_FORM_NAMES = ["waitlist", "waitlist-footer", "quiz-waitlist"]

MAILERLITE_API_KEY = os.getenv("MAILERLITE_API_KEY")
MAILERLITE_GROUP_ID = os.getenv("MAILERLITE_GROUP_ID")   # Waitlist group in MailerLite

EST        = ZoneInfo("America/Toronto")
NOW        = lambda: datetime.now(EST)

VAULT      = Path("C:/Users/User/nova/nova-brain/06_KeepLodge")
WAITLIST_MD   = VAULT / "waitlist.md"
STATE_JSON    = VAULT / "waitlist_state.json"

NETLIFY_API   = "https://api.netlify.com/api/v1"
MAILERLITE_API = "https://connect.mailerlite.com/api"

# ── Obsidian logging ──────────────────────────────────────────────────────────

def ensure_vault():
    VAULT.mkdir(parents=True, exist_ok=True)
    if not WAITLIST_MD.exists():
        WAITLIST_MD.write_text(
            "# KeepLodge Waitlist\n\n"
            "| Date | Name | Email | Properties | Notes |\n"
            "|------|------|-------|------------|-------|\n",
            encoding="utf-8",
        )

def log_signup_to_obsidian(signup: dict):
    ensure_vault()
    date       = NOW().strftime("%Y-%m-%d %H:%M")
    name       = signup.get("name", "Unknown").replace("|", "\\|")
    email      = signup.get("email", "—")
    properties = signup.get("properties", "—")
    notes      = signup.get("notes", "").replace("|", "\\|").replace("\n", " ")
    row        = f"| {date} | {name} | {email} | {properties} | {notes} |\n"

    with open(WAITLIST_MD, "a", encoding="utf-8") as f:
        f.write(row)
    print(f"[waitlist_agent] Logged signup: {name} ({email})")

# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_JSON.exists():
        with open(STATE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "processed_ids": [],
        "total_signups": 0,
        "last_poll":     None,
    }

def save_state(state: dict):
    STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def mark_processed(submission_id: str):
    state = load_state()
    if submission_id not in state["processed_ids"]:
        state["processed_ids"].append(submission_id)
        state["total_signups"] = len(state["processed_ids"])
    state["last_poll"] = NOW().isoformat()
    save_state(state)

def is_processed(submission_id: str) -> bool:
    return submission_id in load_state().get("processed_ids", [])

def get_total_count() -> int:
    return load_state().get("total_signups", 0)

# ── Netlify Forms polling ─────────────────────────────────────────────────────

def get_netlify_form_ids() -> list[tuple[str, str]]:
    """Return list of (name, id) for all tracked forms."""
    headers = {"Authorization": f"Bearer {NETLIFY_TOKEN}"}
    url     = f"{NETLIFY_API}/sites/{NETLIFY_SITE_ID}/forms"

    try:
        resp = requests.get(url, headers=headers, timeout=15, verify=False)
        resp.raise_for_status()
        forms = resp.json()
    except requests.RequestException as e:
        print(f"[waitlist_agent] Netlify forms list error: {e}")
        return []

    matched = [(f["name"], f["id"]) for f in forms if f.get("name") in NETLIFY_FORM_NAMES]
    missing = set(NETLIFY_FORM_NAMES) - {name for name, _ in matched}
    for name in missing:
        print(f"[waitlist_agent] Form '{name}' not found on site {NETLIFY_SITE_ID}")
    return matched

def fetch_new_submissions(form_id: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {NETLIFY_TOKEN}"}
    url     = f"{NETLIFY_API}/forms/{form_id}/submissions"

    try:
        resp = requests.get(url, headers=headers, timeout=15, verify=False)
        resp.raise_for_status()
        submissions = resp.json()
    except requests.RequestException as e:
        print(f"[waitlist_agent] Netlify submissions error: {e}")
        return []

    new = []
    for sub in submissions:
        sub_id = sub.get("id")
        if sub_id and not is_processed(sub_id):
            new.append(sub)

    return new

def parse_submission(raw: dict) -> dict:
    """Normalise a Netlify submission to a clean signup dict."""
    data = raw.get("data", {})
    return {
        "id":         raw.get("id", ""),
        "email":      data.get("email", raw.get("email", "")).strip().lower(),
        "name":       data.get("name", data.get("full_name", "Host")).strip(),
        "properties": data.get("properties", data.get("num_properties", "—")),
        "notes":      data.get("message", data.get("notes", "")),
        "submitted_at": raw.get("created_at", NOW().isoformat()),
    }

# ── MailerLite welcome email ──────────────────────────────────────────────────

def add_to_mailerlite(signup: dict) -> bool:
    """Add subscriber to MailerLite waitlist group and trigger welcome sequence."""
    if not MAILERLITE_API_KEY:
        print("[waitlist_agent] MAILERLITE_API_KEY not set — skipping email")
        return False

    headers = {
        "Authorization": f"Bearer {MAILERLITE_API_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    # Create or update subscriber
    payload = {
        "email": signup["email"],
        "fields": {
            "name":        signup["name"],
            "last_name":   "",
            "properties":  str(signup.get("properties", "")),
        },
        "groups":    [MAILERLITE_GROUP_ID] if MAILERLITE_GROUP_ID else [],
        "status":    "active",
        "resubscribe": True,
    }

    try:
        resp = requests.post(
            f"{MAILERLITE_API}/subscribers",
            headers=headers,
            json=payload,
            timeout=15,
            verify=False,
        )
        if resp.status_code in (200, 201):
            print(f"[waitlist_agent] MailerLite: added {signup['email']}")
            return True
        else:
            print(f"[waitlist_agent] MailerLite error {resp.status_code}: {resp.text[:200]}")
            return False
    except requests.RequestException as e:
        print(f"[waitlist_agent] MailerLite request failed: {e}")
        return False

# ── Main poll cycle ───────────────────────────────────────────────────────────

def poll_and_process() -> int:
    """
    Poll Netlify for new submissions, send welcome emails, log to Obsidian.
    Returns number of new signups processed.
    """
    ensure_vault()
    print(f"[waitlist_agent] Polling at {NOW().strftime('%Y-%m-%d %H:%M EST')}")

    if not NETLIFY_TOKEN or not NETLIFY_SITE_ID:
        print("[waitlist_agent] NETLIFY_TOKEN or NETLIFY_SITE_ID not set — aborting poll")
        return 0

    form_ids = get_netlify_form_ids()
    if not form_ids:
        return 0

    all_submissions = []
    for form_name, form_id in form_ids:
        subs = fetch_new_submissions(form_id)
        print(f"[waitlist_agent] {form_name}: {len(subs)} new submission(s)")
        all_submissions.extend(subs)

    if not all_submissions:
        print("[waitlist_agent] No new submissions across all forms.")
        return 0

    processed = 0

    for raw in all_submissions:
        signup = parse_submission(raw)

        if not signup["email"]:
            print(f"[waitlist_agent] Skipping submission with no email: {raw.get('id')}")
            continue

        # Send welcome email
        email_sent = add_to_mailerlite(signup)
        signup["notes"] = (signup.get("notes") or "") + (
            " | Welcome email sent" if email_sent else " | Email NOT sent"
        )

        # Log to Obsidian
        log_signup_to_obsidian(signup)

        # Mark processed
        mark_processed(signup["id"])
        processed += 1

    state = load_state()
    print(f"[waitlist_agent] Total waitlist count: {state['total_signups']}")
    return processed

# ── Continuous polling mode ───────────────────────────────────────────────────

def run_continuous(interval_seconds: int = 300):
    """
    Poll every `interval_seconds` (default 5 minutes).
    Designed to be run as a background service.
    """
    print(f"[waitlist_agent] Starting continuous poll every {interval_seconds}s. Ctrl+C to stop.")
    while True:
        try:
            poll_and_process()
        except Exception as e:
            print(f"[waitlist_agent] Unexpected error in poll: {e}")
        time.sleep(interval_seconds)

# ── NOVA morning briefing report ─────────────────────────────────────────────

def morning_briefing_report() -> str:
    state = load_state()
    total = state.get("total_signups", 0)
    last  = state.get("last_poll", "never")

    # Count today's signups from Obsidian log
    today       = NOW().strftime("%Y-%m-%d")
    today_count = 0

    if WAITLIST_MD.exists():
        for line in WAITLIST_MD.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"| {today}"):
                today_count += 1

    lines = [
        f"**KeepLodge Waitlist — {today}**",
        f"- Total signups: {total}",
        f"- New today: {today_count}",
        f"- Last poll: {last}",
    ]
    return "\n".join(lines)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--continuous":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 300
        run_continuous(interval)
    elif len(sys.argv) > 1 and sys.argv[1] == "--report":
        print(morning_briefing_report())
    else:
        # Single poll
        new = poll_and_process()
        print(f"\n[waitlist_agent] Poll complete. {new} new signup(s) processed.")
        print("\n" + morning_briefing_report())
