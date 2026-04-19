"""
NOVA Command AI — Claude-powered voice-command intelligence layer.

Replaces keyword-matching command dispatch with an intent classifier that
reads the current utterance, pulls relevant context from the Neural Brain,
and either routes to a structured NOVA action OR returns a free-form spoken
response for Claude to say aloud.

Used from nova_assistant.listen_for_wake_word() — whenever a wake-word
triggers and a user command is captured, pass the transcript here.

Flow:
    transcript --> classify_and_respond(text)
                 ├── retrieves Brain context (recent briefings, trades,
                 │   insights relevant to the utterance)
                 ├── calls Claude with system prompt + context + utterance
                 └── returns CommandResponse {action, spoken, memory_ids}

Actions the classifier can return:
    - STATUS        -> run get_nova_status(), speak live equity + session
    - MORNING_BRIEF -> run morning_briefing() now (out-of-schedule)
    - DEBRIEF       -> run eod_debrief() now
    - LEVELS        -> run /levels command
    - PATTERN       -> run /pattern command
    - REFLECT       -> ask the reflector for fresh insights
    - REMEMBER      -> store the rest of the utterance as a memory
    - RECALL        -> search Brain and speak top hits
    - CHAT          -> free-form response, just speak it
    - UNKNOWN       -> polite clarifier

The Claude call is cheap (Haiku 4.5) and contains at most ~2K input tokens
even with context injection — rough cost ~$0.002/command.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Wire in brain_bridge from the neural-brain package
_BRAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neural-brain", "backend")
if _BRAIN_PATH not in sys.path:
    sys.path.insert(0, _BRAIN_PATH)

try:
    from brain_bridge import (
        context_block,
        remember,
        sync_search,
        sync_online,
    )
    _BRAIN_ENABLED = True
except Exception:
    _BRAIN_ENABLED = False
    def context_block(*_a, **_kw): return ""
    def remember(*_a, **_kw): return None
    def sync_search(*_a, **_kw): return []
    def sync_online(): return False

try:
    from anthropic import Anthropic
    _client = Anthropic()
    _CLAUDE_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))
except Exception:
    _client = None
    _CLAUDE_ENABLED = False


ACTIONS = (
    "STATUS", "MORNING_BRIEF", "DEBRIEF", "LEVELS", "PATTERN", "REFLECT",
    "REMEMBER", "RECALL", "CHAT", "UNKNOWN",
)

ActionName = Literal[
    "STATUS","MORNING_BRIEF","DEBRIEF","LEVELS","PATTERN","REFLECT",
    "REMEMBER","RECALL","CHAT","UNKNOWN",
]


@dataclass
class CommandResponse:
    action:     ActionName
    spoken:     str                # what NOVA says aloud
    payload:    str = ""           # for REMEMBER: the content to store; for RECALL: the query
    reasoning:  str = ""           # Claude's internal rationale (not spoken)
    memory_ids: list[str] | None = None   # ids of Brain memories that informed the answer


# ═══════════════════════════════════════════════════════════════════════════
# Classifier prompt
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """You are NOVA — Sir's personal trading assistant. You run as a
voice-first agent. Every utterance you receive was transcribed from the user's
speech; reply as if speaking back.

Your job: parse the utterance, decide what action (if any) to route to NOVA's
deterministic handlers, and craft a single spoken reply. Your reply is read
aloud by a British-male TTS engine; keep it short, confident, plain-language.
Never say "I'm an AI assistant" or apologise. Address Sir directly.

Available actions (pick exactly one):
- STATUS         — Sir wants current equity / session / today's stats
- MORNING_BRIEF  — Sir wants the full morning briefing run now
- DEBRIEF        — Sir wants the end-of-day debrief run now
- LEVELS         — Sir wants key ICT levels for the current session
- PATTERN        — Sir wants the winning-setup fingerprint from recent trades
- REFLECT        — Sir wants NOVA to reflect on recent activity and surface insights
- REMEMBER       — Sir is telling NOVA to remember something (e.g. "remember that...")
- RECALL         — Sir is asking what he did/decided/said recently. Search memory and recall.
- CHAT           — Free-form conversation, a question with no specific handler
- UNKNOWN        — The utterance is unclear / not meaningful

Output JSON ONLY, in this exact shape:
{
  "action":   "<ONE_OF_THE_ABOVE>",
  "spoken":   "<1-3 sentences NOVA will say aloud>",
  "payload":  "<for REMEMBER: the exact fact to store. for RECALL: the search query. else empty string>",
  "reasoning":"<one line of internal rationale>"
}

Rules:
- Never invent data. If the user asks about something you have no context for,
  say you don't have that yet.
- For RECALL and CHAT, lean on the RELEVANT MEMORIES block in the user prompt.
  If a memory contradicts a confident claim, believe the memory.
- For ambiguous utterances, bias toward CHAT or UNKNOWN — never guess a
  deterministic action.
- No emoji. No unicode flair. No exclamation marks.
"""


# ═══════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════

def classify_and_respond(
    utterance: str,
    model:     str = "claude-haiku-4-5-20251001",
    max_tokens: int = 400,
) -> CommandResponse:
    """
    Main entry point. Returns a CommandResponse.

    If Claude/Brain is unavailable, falls back to keyword matching so NOVA is
    never fully silent.
    """
    text = (utterance or "").strip()
    if not text:
        return CommandResponse("UNKNOWN", "I didn't catch that, Sir.")

    # Fallback path — if Claude/key is unavailable, use the old keyword matcher
    if not (_CLAUDE_ENABLED and _client is not None):
        return _fallback_classifier(text)

    # Build context block from the Brain (always safe — returns "" if offline)
    ctx = ""
    try:
        ctx = context_block(text, limit=5, include_recent=True,
                            header="RELEVANT MEMORIES FROM THE BRAIN")
    except Exception:
        ctx = ""

    user_msg = f"{ctx}\n\n### UTTERANCE\n{text}\n"

    try:
        resp = _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text if resp.content else ""
    except Exception as e:
        return CommandResponse("CHAT",
            "Having trouble reaching my thinking layer, Sir. Try again shortly.",
            reasoning=f"claude error: {e}",
        )

    parsed = _parse_json_response(raw)
    if not parsed:
        return CommandResponse("CHAT", raw[:300] or "Couldn't parse that one, Sir.",
                               reasoning="json parse failure")

    action = parsed.get("action", "UNKNOWN")
    if action not in ACTIONS:
        action = "UNKNOWN"

    return CommandResponse(
        action=action,     # type: ignore[arg-type]
        spoken=parsed.get("spoken", "") or "Done, Sir.",
        payload=parsed.get("payload", "") or "",
        reasoning=parsed.get("reasoning", "") or "",
    )


# ═══════════════════════════════════════════════════════════════════════════
# Post-classification side effects
# ═══════════════════════════════════════════════════════════════════════════

def handle_remember(payload: str) -> bool:
    """Store a user-dictated fact as a Brain memory. Returns success bool."""
    if not payload.strip():
        return False
    mem = remember(
        content=payload,
        summary=payload[:140],
    )
    return bool(mem)


def handle_recall(query: str, limit: int = 3) -> str:
    """Search Brain for a recall query. Returns a spoken-friendly summary."""
    hits = sync_search(query, limit=limit)
    if not hits:
        return f"Nothing in memory about {query}, Sir."
    lines = []
    for m in hits:
        summary = m.get("summary") or (m.get("content") or "")[:120]
        lines.append(summary)
    joined = ". ".join(lines)
    return joined[:500]


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _parse_json_response(raw: str) -> dict | None:
    """Pull the first JSON object out of Claude's response."""
    if not raw:
        return None
    # Prefer a fenced code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: first balanced { ... }
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _fallback_classifier(text: str) -> CommandResponse:
    """Keyword matcher — keeps NOVA functional when Claude / Brain is offline."""
    t = text.lower()
    if any(k in t for k in ("status", "how am i doing", "equity", "loss remaining")):
        return CommandResponse("STATUS", "Pulling current status.")
    if any(k in t for k in ("morning brief", "brief me", "start my day")):
        return CommandResponse("MORNING_BRIEF", "Running the morning brief now.")
    if any(k in t for k in ("debrief", "end of day", "wrap up")):
        return CommandResponse("DEBRIEF", "Running the end of day debrief.")
    if "levels" in t:
        return CommandResponse("LEVELS", "Pulling session levels.")
    if "pattern" in t or "winning setup" in t:
        return CommandResponse("PATTERN", "Pulling the winning setup fingerprint.")
    if "reflect" in t:
        return CommandResponse("REFLECT", "Running reflection now.")
    if t.startswith("remember") or t.startswith("make a note"):
        payload = text.split(" ", 1)[1] if " " in text else ""
        return CommandResponse("REMEMBER", "Got it, Sir.", payload=payload)
    if any(k in t for k in ("what did", "did i", "last", "yesterday", "recall")):
        return CommandResponse("RECALL", "", payload=text)
    return CommandResponse("CHAT", "I'm listening, Sir.")
