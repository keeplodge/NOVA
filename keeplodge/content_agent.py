#!/usr/bin/env python3
"""
KeepLodge Content Agent
Generates daily Instagram and Facebook post drafts.
Saves to nova-brain/06_KeepLodge/content/ for Sir to approve.
Never posts automatically.
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

EST        = ZoneInfo("America/Toronto")
NOW        = lambda: datetime.now(EST)
VAULT      = Path("C:/Users/User/nova/nova-brain/06_KeepLodge")
CONTENT_DIR = VAULT / "content"

# Brand palette (for reference in prompts and metadata)
BRAND = {
    "dark_brown": "#3a2210",
    "cream":      "#f5efe6",
    "burnt_orange": "#c94f0a",
    "voice": (
        "Premium, warm, and human. Write like a trusted advisor who has helped "
        "hundreds of STR hosts — not a marketer. Use plain language. No hype. "
        "No exclamation marks unless truly needed. Lead with insight, empathy, or "
        "a surprising truth about STR hosting."
    ),
}

# ── Content pillars ───────────────────────────────────────────────────────────

CONTENT_PILLARS = {
    "str_tips": {
        "label": "STR Tips",
        "description": "Practical, actionable tips for running a short-term rental — pricing, photos, guest experience, listing optimisation.",
        "weight": 3,
    },
    "host_pain_points": {
        "label": "Host Pain Points",
        "description": "Empathetic content addressing the real frustrations of STR hosts: Airbnb algorithm changes, hidden fees, policy volatility, guest disputes.",
        "weight": 3,
    },
    "direct_booking": {
        "label": "Direct Booking Education",
        "description": "Why and how STR hosts should build direct booking systems. Reduce dependence on OTAs. Own your guest relationships.",
        "weight": 2,
    },
    "keeplodge_story": {
        "label": "KeepLodge Story",
        "description": "Behind-the-scenes, product philosophy, host success stories. Build brand trust without overt selling.",
        "weight": 1,
    },
    "mindset": {
        "label": "Host Mindset",
        "description": "Business mindset content for STR operators — thinking like an owner, long-term thinking, scaling with intention.",
        "weight": 1,
    },
}

PLATFORM_CONFIGS = {
    "instagram": {
        "caption_max_words": 150,
        "hashtag_count": 10,
        "format_note": "Hook in line 1. Two line breaks after hook. Conversational paragraphs. Hashtags at end.",
        "cta_style": "subtle — invite saves, shares, or comments with a question",
    },
    "facebook": {
        "caption_max_words": 200,
        "hashtag_count": 3,
        "format_note": "Longer paragraphs allowed. More personal tone. No hashtag walls.",
        "cta_style": "invite comment or tag a fellow host",
    },
}

# ── Prompt builder ────────────────────────────────────────────────────────────

def build_content_prompt(pillar_key: str, platform: str, angle: str = "") -> str:
    pillar   = CONTENT_PILLARS[pillar_key]
    platform_cfg = PLATFORM_CONFIGS[platform]

    angle_line = f"Specific angle or topic: {angle}" if angle else "Choose a specific, non-generic angle within this pillar."

    return f"""
You are writing social media content for KeepLodge — a premium direct booking platform for STR hosts.

Brand voice: {BRAND['voice']}
Brand colours (reference only — describe visually if needed): dark brown {BRAND['dark_brown']}, cream {BRAND['cream']}, burnt orange {BRAND['burnt_orange']}.

Content pillar: {pillar['label']}
Pillar description: {pillar['description']}
{angle_line}

Platform: {platform.upper()}
Format guidance: {platform_cfg['format_note']}
Max caption length: {platform_cfg['caption_max_words']} words
Hashtag count: {platform_cfg['hashtag_count']}
CTA style: {platform_cfg['cta_style']}

Rules:
- Never use salesy or hype language
- No exclamation marks in the first line
- Do not mention competitor OTAs by name unless absolutely necessary for context
- Never promise specific revenue figures
- Write like a knowledgeable human — warm, direct, real
- Include a visual suggestion (one sentence describing the ideal image or graphic)

Output format (JSON only):
{{
  "platform": "{platform}",
  "pillar": "{pillar_key}",
  "hook": "first line of caption",
  "caption": "full post caption including hashtags",
  "visual_suggestion": "description of ideal image or graphic",
  "internal_notes": "any notes for Sir on timing, context, or boost potential"
}}

Respond with JSON only — no markdown, no explanation.
"""

# ── Content generation ────────────────────────────────────────────────────────

def weighted_pillar_choice() -> str:
    pool = []
    for key, cfg in CONTENT_PILLARS.items():
        pool.extend([key] * cfg["weight"])
    return random.choice(pool)

def generate_post(platform: str, pillar_key: str = None, angle: str = "") -> dict:
    if pillar_key is None:
        pillar_key = weighted_pillar_choice()

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt   = build_content_prompt(pillar_key, platform, angle)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"```$", "", raw).strip()

    try:
        post = json.loads(raw)
    except json.JSONDecodeError:
        post = {
            "platform":        platform,
            "pillar":          pillar_key,
            "hook":            "Draft could not be parsed",
            "caption":         raw,
            "visual_suggestion": "",
            "internal_notes":  "JSON parse error — review raw output",
        }

    post["generated_at"] = NOW().isoformat()
    post["status"]       = "draft"
    return post

def generate_daily_batch() -> list[dict]:
    """
    Generate one Instagram post and one Facebook post for today.
    Weighted pillar selection ensures variety.
    """
    posts = []

    for platform in ("instagram", "facebook"):
        pillar_key = weighted_pillar_choice()
        print(f"[content_agent] Generating {platform} post — pillar: {pillar_key}")
        post = generate_post(platform, pillar_key)
        posts.append(post)

    return posts

# ── Save to Obsidian ──────────────────────────────────────────────────────────

def save_draft(post: dict) -> Path:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    date_str = NOW().strftime("%Y-%m-%d")
    platform = post.get("platform", "unknown")
    pillar   = post.get("pillar", "general")
    slug     = f"{date_str}_{platform}_{pillar}"
    filepath = CONTENT_DIR / f"{slug}.md"

    # If file exists, append a counter
    counter = 1
    while filepath.exists():
        filepath = CONTENT_DIR / f"{slug}_{counter}.md"
        counter += 1

    lines = [
        f"# KeepLodge {platform.title()} Draft — {date_str}",
        f"**Pillar:** {CONTENT_PILLARS.get(pillar, {}).get('label', pillar)}",
        f"**Status:** {post.get('status', 'draft')}  ",
        f"**Generated:** {post.get('generated_at', '')}",
        "",
        "---",
        "",
        f"## Hook",
        post.get("hook", ""),
        "",
        "## Caption",
        "```",
        post.get("caption", ""),
        "```",
        "",
        "## Visual Suggestion",
        post.get("visual_suggestion", ""),
        "",
        "## Internal Notes",
        post.get("internal_notes", ""),
        "",
        "---",
        "*Awaiting Sir's approval before posting.*",
    ]

    filepath.write_text("\n".join(lines), encoding="utf-8")
    print(f"[content_agent] Draft saved: {filepath.name}")
    return filepath

def save_batch(posts: list[dict]) -> list[Path]:
    return [save_draft(p) for p in posts]

# ── Index file ────────────────────────────────────────────────────────────────

def update_content_index():
    """Rebuild content/index.md listing all drafts."""
    files = sorted(CONTENT_DIR.glob("*.md"), reverse=True)
    files = [f for f in files if f.name != "index.md"]

    lines = ["# KeepLodge Content Drafts\n"]
    for f in files:
        date  = f.stem[:10] if len(f.stem) >= 10 else f.stem
        label = f.stem.replace("_", " ").title()
        lines.append(f"- [[{f.stem}]] — {date}")

    index_path = CONTENT_DIR / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[content_agent] Content index updated ({len(files)} drafts)")

# ── Daily summary for NOVA briefing ──────────────────────────────────────────

def daily_content_summary() -> str:
    today    = NOW().strftime("%Y-%m-%d")
    drafts   = list(CONTENT_DIR.glob(f"{today}_*.md"))
    count    = len(drafts)
    names    = [d.stem for d in drafts]

    if count == 0:
        return f"KeepLodge Content: No drafts generated today ({today})."

    lines = [f"**KeepLodge Content — {today}**", f"- {count} draft(s) ready for approval"]
    for name in names:
        lines.append(f"  - {name}")
    lines.append("- Location: nova-brain/06_KeepLodge/content/")
    return "\n".join(lines)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[content_agent] Generating daily KeepLodge content batch...\n")

    posts = generate_daily_batch()
    paths = save_batch(posts)
    update_content_index()

    print("\n" + daily_content_summary())
    print("\n[content_agent] Done. All drafts await Sir's approval — nothing posted.")
