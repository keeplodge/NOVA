#!/usr/bin/env python3
"""
KeepLodge Lead Agent
Monitors Facebook STR group URLs, drafts natural outreach messages,
tracks conversation status, and logs leads to Obsidian.
"""

import json
import os
import random
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN")

EST = ZoneInfo("America/Toronto")
NOW = lambda: datetime.now(EST)

VAULT = Path("C:/Users/User/nova/nova-brain/06_KeepLodge")
LEADS_MD = VAULT / "leads.md"
STATUS_JSON = VAULT / "lead_status.json"

# Facebook STR group URLs to monitor
TARGET_GROUPS = [
    # Add real group IDs or URLs here — e.g.:
    # "https://www.facebook.com/groups/airbnbhosts",
    # "https://www.facebook.com/groups/stroperators",
]

# Conversation states
STATUS_NEW       = "new"
STATUS_DRAFTING  = "drafting"
STATUS_WARM      = "warm"
STATUS_DM        = "dm"
STATUS_CONVERTED = "converted"
STATUS_DEAD      = "dead"

# ── Outreach rules enforced in every prompt ────────────────────────────────────

OUTREACH_RULES = """
KeepLodge outreach rules — follow every one without exception:
1. Never use salesy or pushy language. No "limited offer", "act now", "best deal".
2. Sound like a real person having a conversation, not a marketer.
3. Spend time in the group engaging genuinely before any outreach post.
4. Never post the same message twice — each draft must be unique.
5. Reference something specific from the host's situation or post.
6. Never pitch immediately — lead with value, empathy, or a question.
7. Move warm leads to DMs naturally: "happy to share more in DM if helpful."
8. Never name-drop KeepLodge in cold messages — let curiosity do the work.
9. Brand voice: premium, warm, human. Not corporate. Not pushy.
10. If uncertain about a host's situation, ask a question rather than assume.
"""

# ── Obsidian logging ──────────────────────────────────────────────────────────

def ensure_vault():
    VAULT.mkdir(parents=True, exist_ok=True)
    (VAULT / "content").mkdir(exist_ok=True)
    if not LEADS_MD.exists():
        LEADS_MD.write_text(
            "# KeepLodge Leads\n\n"
            "| Date | Name | Group | Status | Notes |\n"
            "|------|------|-------|--------|-------|\n",
            encoding="utf-8",
        )

def log_lead_to_obsidian(lead: dict):
    ensure_vault()
    date    = NOW().strftime("%Y-%m-%d")
    name    = lead.get("name", "Unknown")
    group   = lead.get("group", "—")
    status  = lead.get("status", STATUS_NEW)
    notes   = lead.get("notes", "").replace("|", "\\|").replace("\n", " ")
    row     = f"| {date} | {name} | {group} | {status} | {notes} |\n"

    with open(LEADS_MD, "a", encoding="utf-8") as f:
        f.write(row)
    print(f"[lead_agent] Logged lead: {name} ({status})")

def append_obsidian_note(content: str):
    ensure_vault()
    with open(LEADS_MD, "a", encoding="utf-8") as f:
        f.write(f"\n---\n{content}\n")

# ── Lead status tracker ───────────────────────────────────────────────────────

def load_status() -> dict:
    if STATUS_JSON.exists():
        with open(STATUS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_status(data: dict):
    STATUS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def upsert_lead(lead_id: str, updates: dict):
    data = load_status()
    if lead_id not in data:
        data[lead_id] = {
            "id": lead_id,
            "status": STATUS_NEW,
            "created_at": NOW().isoformat(),
            "messages_sent": [],
        }
    data[lead_id].update(updates)
    data[lead_id]["updated_at"] = NOW().isoformat()
    save_status(data)
    return data[lead_id]

def get_all_leads() -> list:
    return list(load_status().values())

def get_leads_by_status(status: str) -> list:
    return [l for l in get_all_leads() if l.get("status") == status]

# ── Message uniqueness check ──────────────────────────────────────────────────

def already_sent(lead_id: str, message: str) -> bool:
    data = load_status()
    lead = data.get(lead_id, {})
    sent = lead.get("messages_sent", [])
    # Simple similarity: strip whitespace and compare lowercased
    clean = re.sub(r"\s+", " ", message.lower().strip())
    for prev in sent:
        prev_clean = re.sub(r"\s+", " ", prev.lower().strip())
        if clean == prev_clean:
            return True
        # Fuzzy: if 90%+ of words overlap flag it
        words_new  = set(clean.split())
        words_prev = set(prev_clean.split())
        if len(words_new) > 0:
            overlap = len(words_new & words_prev) / len(words_new)
            if overlap > 0.9:
                return True
    return False

def record_sent_message(lead_id: str, message: str):
    data = load_status()
    if lead_id not in data:
        data[lead_id] = {"id": lead_id, "messages_sent": []}
    data[lead_id].setdefault("messages_sent", []).append(message)
    save_status(data)

# ── Claude message drafting ───────────────────────────────────────────────────

def draft_outreach_message(host_context: dict, lead_id: str, attempt: int = 0) -> str:
    if attempt > 3:
        raise RuntimeError(f"Could not generate a unique message for lead {lead_id} after 3 attempts")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    name         = host_context.get("name", "this host")
    post_snippet = host_context.get("post_snippet", "")
    pain_points  = host_context.get("pain_points", [])
    group_name   = host_context.get("group", "an STR group")
    stage        = host_context.get("status", STATUS_NEW)

    pain_str = ", ".join(pain_points) if pain_points else "general STR challenges"

    if stage == STATUS_WARM:
        direction = (
            "This host has already engaged positively. "
            "Naturally invite them to continue the conversation in DMs. "
            "Be warm, casual, and low-pressure."
        )
    else:
        direction = (
            "This is a cold touch in a Facebook group. "
            "Do NOT mention KeepLodge by name. "
            "Lead with empathy or a helpful observation about their situation. "
            "End with an open question or light offer to help."
        )

    prompt = f"""
{OUTREACH_RULES}

You are writing on behalf of a premium direct booking platform called KeepLodge.
Context about this host:
- Name: {name}
- Group: {group_name}
- Their post or situation: {post_snippet}
- Apparent pain points: {pain_str}
- Current relationship stage: {stage}

Direction: {direction}

Write ONE short, natural outreach message (3-5 sentences max).
Do not include subject lines, greetings like "Hi [Name]", or sign-offs.
Just the body of the message, ready to paste.
Output only the message text — nothing else.
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )

    message = response.content[0].text.strip()

    if already_sent(lead_id, message):
        print(f"[lead_agent] Duplicate detected, regenerating (attempt {attempt + 1})")
        return draft_outreach_message(host_context, lead_id, attempt + 1)

    return message

# ── Facebook group monitoring (scaffold) ─────────────────────────────────────
# Full Facebook Graph API integration requires app review for group content access.
# This scaffold processes posts fed in from a manual export or a webhook.

def process_group_post(post: dict) -> dict | None:
    """
    Analyse a raw Facebook group post and decide if it's worth outreach.
    Returns a lead dict if actionable, None if not.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    text = post.get("message", "")
    if len(text) < 30:
        return None

    prompt = f"""
You are screening Facebook posts in STR (short-term rental) host groups for KeepLodge.

KeepLodge helps STR hosts reduce their dependence on Airbnb by building direct booking systems.

Analyse this post and respond in JSON only:
{{
  "is_lead": true/false,
  "name": "poster name or Unknown",
  "pain_points": ["list", "of", "pain points"],
  "post_snippet": "one-sentence summary of their situation",
  "confidence": 0-10
}}

Only set is_lead=true if the host shows signs of:
- Frustration with Airbnb fees or policies
- Wanting more direct bookings
- Looking for tools or help managing their property
- Growing or scaling their portfolio

Post:
\"\"\"{text}\"\"\"

Respond with JSON only.
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
        result = json.loads(raw)
    except (json.JSONDecodeError, IndexError):
        return None

    if not result.get("is_lead") or result.get("confidence", 0) < 5:
        return None

    return {
        "name":         result.get("name", "Unknown"),
        "group":        post.get("group_name", "Unknown Group"),
        "post_snippet": result.get("post_snippet", ""),
        "pain_points":  result.get("pain_points", []),
        "status":       STATUS_NEW,
        "post_id":      post.get("id", ""),
        "post_url":     post.get("permalink_url", ""),
        "raw_text":     text[:500],
    }

# ── Main workflow ─────────────────────────────────────────────────────────────

def run_lead_scan(posts: list[dict]):
    """
    Feed a list of raw Facebook post dicts to scan for leads.
    Each post dict should have: id, message, group_name, permalink_url, from.name
    """
    print(f"[lead_agent] Scanning {len(posts)} posts for leads...")
    new_leads = 0

    for post in posts:
        post.setdefault("from", {})
        post["group_name"] = post.get("group_name", "Unknown Group")

        lead_data = process_group_post(post)
        if not lead_data:
            continue

        lead_id = post.get("id") or f"lead_{NOW().strftime('%Y%m%d%H%M%S')}_{random.randint(1000,9999)}"
        existing = load_status().get(lead_id)

        if existing and existing.get("status") in (STATUS_DM, STATUS_CONVERTED, STATUS_DEAD):
            continue  # Skip already actioned leads

        upsert_lead(lead_id, lead_data)
        log_lead_to_obsidian({**lead_data, "notes": lead_data.get("post_snippet", "")})

        # Draft outreach message
        try:
            message = draft_outreach_message(lead_data, lead_id)
            upsert_lead(lead_id, {
                "status":          STATUS_DRAFTING,
                "draft_message":   message,
            })
            print(f"\n[lead_agent] Lead: {lead_data['name']}")
            print(f"  Pain points: {', '.join(lead_data.get('pain_points', []))}")
            print(f"  Draft message:\n  {message}\n")
        except RuntimeError as e:
            print(f"[lead_agent] Warning: {e}")

        new_leads += 1

    print(f"[lead_agent] Scan complete. {new_leads} new leads found.")
    return new_leads

def promote_warm_leads():
    """Draft DM transition messages for warm leads."""
    warm = get_leads_by_status(STATUS_WARM)
    print(f"[lead_agent] Processing {len(warm)} warm leads for DM transition...")

    for lead in warm:
        lead_id = lead["id"]
        try:
            message = draft_outreach_message(lead, lead_id)
            upsert_lead(lead_id, {"dm_draft": message, "status": STATUS_DM})
            print(f"[lead_agent] DM draft ready for {lead.get('name', lead_id)}")
            print(f"  {message}\n")
        except RuntimeError as e:
            print(f"[lead_agent] Warning: {e}")

def daily_lead_summary() -> str:
    all_leads  = get_all_leads()
    counts     = {}
    for l in all_leads:
        s = l.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    lines = [f"**KeepLodge Lead Summary — {NOW().strftime('%Y-%m-%d')}**"]
    for status, count in sorted(counts.items()):
        lines.append(f"- {status}: {count}")
    lines.append(f"- Total: {len(all_leads)}")

    summary = "\n".join(lines)
    append_obsidian_note(summary)
    return summary

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_vault()

    # Example: feed in scraped or exported posts
    sample_posts = [
        {
            "id": "sample_001",
            "message": (
                "Airbnb just cut my visibility again after I declined a booking. "
                "This is the third time this month. I have 4 properties and I'm so "
                "tired of being at their mercy. Has anyone had success getting direct bookings? "
                "I don't even know where to start building my own website."
            ),
            "group_name": "Airbnb Hosts Community",
            "permalink_url": "https://facebook.com/groups/example/posts/sample_001",
            "from": {"name": "Sarah M."},
        },
    ]

    run_lead_scan(sample_posts)
    promote_warm_leads()
    print("\n" + daily_lead_summary())
