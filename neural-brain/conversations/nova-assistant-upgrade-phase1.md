# NOVA Assistant Upgrade — Phase 1 Shipped (2026-04-19)

Advanced the NOVA Assistant from a stateless daemon into a memory-backed, Claude-routed voice agent. The Assistant now remembers every briefing and debrief as a structured Neural Brain memory, recalls yesterday's context at the start of each morning briefing, and routes every voice command through a Claude Haiku 4.5 intent classifier with Brain context injected — no more keyword matching. Phase 2 (reflector extension + drift monitor + adaptive waveform) flagged for next session. This memory is the canonical record of what moved.

## What shipped tonight

### 1. Enhanced `brain_bridge.py` — proper memory SDK

Rewrote the bridge as a real SDK rather than a thin HTTP wrapper. Backwards compatible with the old `sync_store`/`sync_search`/`classify` API, but now exposes:
- `remember()` — write a memory with smart defaults (auto-classification, tag extraction, optional heading prepend)
- `remember_briefing(date, content, summary)` — tagged `nova:briefing:<date>`
- `remember_debrief(date, content, summary)` — tagged `nova:debrief:<date>`
- `remember_trade(date, ticker, action, outcome, session, grade, notes)` — tagged `trading:trade:<date>`
- `remember_insight(topic, content, category)` — tagged `<category>:insight:<topic>`
- `recent_filtered(category, hours, limit)` — time-windowed recency retrieval
- `context_block(query, category, limit, include_recent, header)` — returns a ready-to-inject system-prompt block summarising relevant memories for a query. Safe to concat unconditionally; returns empty string if Brain is offline.
- `sync_online()` — health check

All helpers have async + sync variants. Classification rules expanded to cover `trading`, `keeplodge`, `probuild`, `nova`, `ideas`, `personal`, `general`.

### 2. Memory wiring in `nova_assistant.py`

- **Morning briefing recall** (new Section 0): at the start of each morning briefing, before any market data, NOVA queries the Brain for `debrief {yesterday}` and speaks a one-line recap. If nothing found, falls back to a broader "yesterday trade session loss setup" search. Gracefully skips if Brain is offline.
- **Morning briefing store**: replaced the inline `_brain_store` call with `_brain_remember_briefing(date_str, content, summary)`. Content is now a multi-line structured block including energy, NQ price, VIX + rating, mindset check-in, priorities, and discipline reminder — far richer than the previous one-liner. Auto-tagged with `nova:briefing:<YYYY-MM-DD>`.
- **EOD debrief store** (new): appends a structured memory at the end of `eod_debrief()` covering trades today, daily loss, risk budget remaining, per-session trade counts. Tagged `nova:debrief:<YYYY-MM-DD>`.

### 3. `nova_command_ai.py` (NEW) — Claude-powered voice intent classifier

Complete new module. Every voice command now flows through `classify_and_respond(utterance)` which:
- Pulls `context_block(utterance, limit=5)` from the Brain as system-prompt context
- Calls Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) with a strict JSON output schema
- Returns `CommandResponse(action, spoken, payload, reasoning, memory_ids)`

Supported actions: `STATUS`, `MORNING_BRIEF`, `DEBRIEF`, `LEVELS`, `PATTERN`, `REFLECT`, `REMEMBER`, `RECALL`, `CHAT`, `UNKNOWN`. Each has a deterministic handler in `nova_assistant._dispatch_command_action()` OR is a free-form spoken reply.

If Claude/ANTHROPIC_API_KEY is not available, falls back to a keyword matcher so voice never fully dies. Rough cost per command: ~$0.002 (Haiku). At 20 voice commands/day that's $14/year.

### 4. `listen_for_wake_word()` rewrite

Previously only spoke "Sir. Ready. What do you need?" and did not capture the follow-up utterance. Now:
1. Detects wake phrase
2. Speaks "Sir."
3. Captures the command via `listen_response(timeout=8)`
4. Classifies via `_nova_classify(command)` if enabled, keyword fallback otherwise
5. Speaks the classifier's `response.spoken`
6. Dispatches the structured `response.action` to `_dispatch_command_action()`

New `_dispatch_command_action()` implementations:
- `STATUS` → pulls `get_nova_status()` and speaks session + trades + loss
- `MORNING_BRIEF` / `DEBRIEF` → spawns the function in a daemon thread (non-blocking)
- `REMEMBER` → persists payload to Brain via `_nova_cmd_remember()`
- `RECALL` → searches Brain via `_nova_cmd_recall()`, speaks top 3 summaries
- `PATTERN` → spawns `nova_pattern_agent.py` subprocess
- `REFLECT` → POSTs `/insights/run` to the Brain's reflector endpoint
- `LEVELS` → placeholder (future session wires the `/levels` skill)
- `CHAT` / `UNKNOWN` → just speaks the classifier's reply

## What's intentionally deferred to Phase 2

- **Reflector extension for NOVA trading data** — the existing `reflector.py` only synthesises insights from general memories and Obsidian trade logs. Phase 2 adds a category-filtered `reflect_on_nova_trading()` that runs separately and produces `trading:insight:*` memories.
- **Adaptive waveform dashboard** in `nova_local.py` — Tkinter canvas bars should react to live VIX + daily-loss + session state. Still static today.
- **Strategy drift detection monitor** — standalone `nova_drift_monitor.py` that polls trade history and fires a voice alert when win-rate drops >5% MoM.
- **MCP standardization of external APIs** — weather, Finnhub, NewsAPI, yfinance wrapped as MCP tools (currently raw HTTP in nova_assistant.py).

## Open prerequisite

- `ANTHROPIC_API_KEY` is NOT yet in `C:\Users\User\nova\.env`. Until Sir adds it, `nova_command_ai.py` runs in keyword-fallback mode (still functional, but won't route "how did I do yesterday" to RECALL — only keyword-matching utterances route correctly). Add the key and the upgrade flips to full Claude routing with zero code change.

## Smoke tests passed

- `ast.parse()` of all three modified/new files — clean
- `brain_bridge.sync_online()` → True (Brain responded on port 7337)
- `remember_briefing()` write → returned memory id 89623274
- `sync_search('smoke test briefing')` → 1 hit (round-trip confirmed)
- `context_block()` → 236-char formatted block with 3 relevant memories
- `classify_and_respond('what's my status')` with Claude disabled → `action=STATUS, spoken="Pulling current status."` via keyword fallback (confirms graceful degradation)

## Files touched

- `C:\Users\User\nova\neural-brain\backend\brain_bridge.py` — full rewrite, backwards-compatible API
- `C:\Users\User\nova\nova_command_ai.py` — new module (~200 lines)
- `C:\Users\User\nova\nova_assistant.py` — 4 surgical edits (import block, Section 0 briefing recall, briefing store upgrade, debrief store, `listen_for_wake_word` + `_dispatch_command_action`)

## Rules for future sessions

- `nova_command_ai._SYSTEM_PROMPT` is the canonical voice-command classifier prompt. Edit carefully — every voice interaction routes through it.
- The JSON output schema is strict: `{action, spoken, payload, reasoning}`. Don't add fields without updating `CommandResponse` dataclass + `_parse_json_response` + all dispatch handlers.
- Any new ACTION requires: (1) add to `ACTIONS` tuple, (2) add to `_SYSTEM_PROMPT` action list, (3) add handler to `_dispatch_command_action` in nova_assistant.
- brain_bridge helper additions should follow the same shape as `remember_briefing` — preferred heading tag, auto-classified category, structured tags list.
- Never bypass brain_bridge and hit `/memory` directly from other scripts. One SDK, one convention.
