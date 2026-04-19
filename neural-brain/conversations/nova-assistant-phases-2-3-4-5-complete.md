# NOVA Assistant — Phases 2, 3, 4, 5 Complete (2026-04-19)

Ships the remaining NOVA Assistant upgrade in one session: trading-specific reflector, adaptive waveform tinted from live market data, strategy drift monitor, and a dramatic visual facelift of `nova_local.py` that mirrors the Neural Brain's design-system aesthetic within Tkinter's capability envelope. This memory is the canonical record of what moved across phases 2-5 so future sessions can verify state without re-reading commits.

## Phase 2 — Trading Reflector

Extended `neural-brain/backend/reflector.py` with a `run_trading_reflection()` function that:
- Pulls trading-category memories from the last 14 days (was 24 hours for the general reflector)
- Pulls Obsidian trade logs from `01_Trade_Logs/` via a new `_recent_trade_logs_days()` helper
- Uses a dedicated `TRADING_REFLECTION_PROMPT` focused on session-by-session pattern recognition, drawdown flags, grade-timing correlations, and mindset/win-rate links
- Forces insight category to `trading` regardless of model output

`scheduler_loop()` became dual-slot: fires both the nightly general reflection at 22:00 EST and a pre-Asia trading reflection at 18:00 EST. Picks the nearest upcoming target each iteration.

New REST endpoint `POST /insights/run-trading` on the FastAPI server for manual triggering.

## Phase 3 — Adaptive Waveform

`nova_local.py` now has a `market_state_poller_loop` background thread that polls VIX (yfinance `^VIX`) and NOVA Railway `/status` every 60 seconds. Drives two things:

1. **Waveform idle tint** — colors the orb + bars by state priority:
   - `daily_loss >= 300` → RED (loss warning)
   - `loss_remaining <= 200` → ORANGE_WARN (near cap)
   - active session → PURPLE_LIVE
   - VIX ≥ 25 → AMBER (elevated)
   - VIX ≤ 13 → TEAL_CALM
   - default → CYAN

2. **Side stat panels** (live_state patches via `_push_state()`): session, NQ price, VIX, daily_loss, remaining, trades_today, brain_status, brain_memories — all update every 60s from the poller.

New helpers: `_push_color_only()` (update color without changing mode), `_push_state()` (merge a live_state patch). The GUI queue message schema now accepts a `state` key as a dict of live_state patches.

## Phase 4 — Drift Monitor

New module `nova_drift_monitor.py`. Background thread polls `nova_brain.db` every 60 minutes. Three independent drift detectors:

- `_check_winrate_drop()` — compares last 20 trades win-rate to prior 20; flags if drop ≥ 5pp (critical at ≥ 15pp)
- `_check_loss_streak()` — flags 3+ consecutive losses (critical at 5+)
- `_check_drawdown()` — cumulative R-multiple over last 10 trades; flags at ≤ -6R (critical at ≤ -8R)

Each alert:
- Speaks a voice warning via the shared `speak()` function
- Writes a `trading:drift:<date>` memory to the Brain tagged with kind + severity
- Uses signature-based dedup so the same drift doesn't re-alert within a day

Graceful degradation: silent no-op if nova_brain.db is missing, Brain is offline, or no `speaker` provided. CLI entry point (`python nova_drift_monitor.py`) for one-shot debugging. Spawned from both `nova_assistant.main()` and `nova_local.main()`.

## Phase 5 — Visual Facelift

Dramatic restyle of `nova_local.py`'s Tkinter GUI to match the Neural Brain's design system. Window grew from 720x560 to 1000x680. Key changes:

- **Background**: `#0A1929` (Blueprint Navy) — matches Brain
- **Blueprint grid**: hairline lines at 48px spacing draw a subtle grid across the entire canvas
- **Header**: orange logo mark (square with "N"), "NOVA" wordmark in Arial Black, "ASSISTANT" subtitle in orange, right-side overline status "NEURAL OPERATIONS · VOICE ASSISTANT · V2"
- **Title**: centered "N · O · V · A" in Arial Black 26pt with layered glow
- **Wireframe orb**: replaces the solid pulsing ellipse. 14 Fibonacci-distributed nodes projected to 2D, each connected to its 3 nearest neighbors (deduped edges). Inner bright core dot (7-11px radius pulse). Concentric glow rings still present but redesigned. Matches the Brain's WebGL wireframe orb within Tkinter's 2D constraints.
- **Side stat panels**: two glass-style cards with orange overline headings and 4 rows of LABEL / value pairs each. Left panel: MARKET STATE (session, NQ, VIX, status). Right panel: TODAY (daily loss, remaining, trades today, memories). Values update every 60s from the market poller.
- **Waveform**: 48 bars (was 36), bounded by the side panels, color follows the current state tint
- **Neural-link strip** at bottom: pulsing green/red dot indicator + "LINKED TO NEURAL BRAIN" label + centered live status + right-aligned "NOVA · V2 · ASSISTANT"
- **Color tokens extended**: `C_BG`, `C_BG_ALT`, `C_BORDER`, `C_GRID`, `C_ORANGE`, `C_STEEL`, `C_STEEL_DARK` added — mirror the Brain's `--pb-*` variables.

Typography uses closest Windows-available substitutes for the Brain's Google Fonts (Arial Black for Archivo Black, Consolas for JetBrains Mono). True parity with the Brain's WebGL + shader + blur effects is impossible in Tkinter and requires an Electron rebuild — flagged as Phase 6 if Sir wants it.

## Files touched this session

- `neural-brain/backend/reflector.py` — extended with trading reflection
- `neural-brain/backend/server.py` — added `/insights/run-trading` endpoint
- `nova_assistant.py` — drift monitor spawn in main()
- `nova_drift_monitor.py` — NEW module (~200 lines)
- `nova_local.py` — adaptive poller + visual facelift (~300 net new lines)

## Smoke tests passed

- All 5 files pass `ast.parse()`
- `nova_drift_monitor.py` CLI: 1 open trade in DB, 0 closed → 0 alerts (expected)
- `brain_bridge.sync_online()` → True, `_brain_stats()` returns ("ONLINE", "56")

## Deferred to Phase 6 (flagged but NOT in this session)

- **Electron rebuild of nova_local** for true Brain parity — WebGL orb with real shader deformation, backdrop-filter glass panels, 60fps animation. The Tkinter facelift is the most one-session Python-only approach can match visually.
- **MCP standardization of external APIs** — weather, Finnhub, NewsAPI, yfinance as MCP tools
- **Bidirectional WebSocket bridge** — so the Brain's UI can display assistant state in real time (currently unidirectional Python → Tkinter)

## Prerequisites still open

- `ANTHROPIC_API_KEY` not yet in `.env` — `nova_command_ai.py` in keyword-fallback mode until added
- Real closed trades in `nova_brain.db` for drift checks to produce actionable signal
- Ollama `llama3.2:3b` running for trading reflections to actually emit insights

## Commits from this session

- `7f0b8eb` — Phase 1: Brain memory + Claude-routed voice
- `863768b` — Phases 2/3/4: trading reflector, drift monitor, adaptive waveform
- (this session end) — Phase 5: visual facelift

## Rules for future sessions

- The `live_state` dict in `NOVAApp` is the canonical source for stat-panel values. Update via `_push_state(patch)`, not by mutating directly.
- `_push_color_only(color, status)` updates tint without touching mode — safe to call during listening/speaking without disrupting their animations.
- Drift monitor thresholds (5pp, 3-streak, -6R) are empirical starting points. Tune in `nova_drift_monitor.py` constants as real trade data accumulates.
- If Electron rebuild is pursued (Phase 6), preserve the `_gui_queue` message schema — the Python side is already abstracted from the rendering layer. Swap the Tkinter consumer for a WebSocket publisher.
- Never bypass `brain_bridge` to write memories — all persistence goes through the SDK.
