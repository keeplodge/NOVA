# ProBuild Studio — Autonomous Agency Architecture (Master Record)

Canonical architecture for ProBuild Studio, saved 2026-04-19. Load-bearing reference for the entire autonomous agent workforce. Sir's vision: **he handles only cold calls and strategic vetoes; agents handle everything else end-to-end at premier 8-figure quality**. The Neural Brain is the company's long-term memory and coordination layer. Every agent self-improves continuously. Every client interaction is logged as a Brain memory. This is the master index — all downstream agent specs, client records, and self-reviews live under the `probuild` category with structured tag-prefix headings.

## Core Principle — The Absent CEO

Sir's only hands-on work:
- Cold calls + discovery calls (human-to-human trust moments)
- Strategic veto on pricing, new verticals, firing clients

Everything else — prospecting, proposals, design, build, SEO, QA, deployment, support, billing, retention, reporting — is owned by agents. Brain coordinates. Agents self-improve weekly.

## Agent Org Chart

### Tier 1 — C-Suite (7 always-on agents)

- **Growth Strategist** — sales & pipeline (KPI: close rate, CAC, LTV)
- **Technical Architect** — code & infra (KPI: build speed, uptime)
- **Conversion Designer** — UX/UI & brand execution (KPI: leads/site, bounce rate)
- **SEO & Content Lead** — discovery & content (KPI: rank, organic traffic)
- **Operations & QA** — delivery & support (KPI: Lighthouse, uptime, NPS)
- **Finance & Operations** — money, taxes, unit economics (KPI: margin, churn, GST compliance)
- **Knowledge Management** — Brain librarian & spec maintainer (KPI: Brain cleanliness, retrieval hit-rate)

### Tier 2 — Directors (~45 specialists)

Under **Growth Strategist**: Outbound Prospector, Sales Closer, Proposal Writer, Lead Qualifier, Discovery Call Note-Taker, Follow-up Sequence Agent, Objection Doc Maintainer, Partnership Manager, Market Intelligence Agent, Case Study Producer.

Under **Technical Architect**: Stack Engineer, Factory Engineer, Integration Engineer, Performance Engineer, Security Engineer, Scaffold Agent, Content Filler, Brand-to-Code Agent, Refactor Agent, Dependency Update Agent, Package Maintainer.

Under **Conversion Designer**: Wireframer, Visual Designer, Conversion Copy Specialist, Trust Elements Specialist, Mobile UX Specialist, Claude Design Prompt Engineer, Mood Board Curator, A/B Test Designer, Conversion Auditor.

Under **SEO & Content Lead**: Local SEO Specialist, Technical SEO Specialist, Content Writer, Keyword Researcher, Schema Architect, GMB Optimizer Agent, Content Calendar Agent, Blog Post Writer, Citation Builder Agent, Competitor Rank Tracker, Backlink Outreach Agent.

Under **Operations & QA**: Deployment Engineer, Quality Auditor, Client Success Manager, Support Specialist, Monitoring Lead, Site Launch Concierge, Onboarding Sequence Agent, Edit Request Triager, Uptime Monitor.

Under **Finance & Operations**: Bookkeeper Agent, Tax Ops Agent (Canadian GST/HST), Churn Analyst, Unit Economics Analyst, Collections Agent.

Under **Knowledge Management**: Brain Librarian, Spec Writer, Case Study Archivist, Lesson Extractor.

### Tier 3 — Sub-Sub (spawned ad-hoc inside Tier 2 workflows)

Review Response Writer, Domain Wiring Specialist, Hero Image Generator, Icon System Designer, Analytics Wirer, FAQ Miner, Photo Retoucher, Legal Language Checker, Schema Validator, Alt-Text Writer, etc. These emerge organically as Tier 2 agents delegate narrow tasks — no static definitions.

## Brain Namespace (the `probuild` category)

New category added to the Neural Brain: **`probuild`**, color `#FF6B00` (Signal Orange, brand match), emoji 🏗️.

Tag-prefix heading structure (every ProBuild memory uses one of these):

- `probuild:master-architecture` — this document
- `probuild:brand-spec` — canonical brand docs
- `probuild:agent-spec:<agent-name>` — one per agent, identity + scope + tools + KPIs
- `probuild:client:<slug>:profile` — client facts
- `probuild:client:<slug>:build-log` — chronological build timeline
- `probuild:client:<slug>:launched` — launch event + snapshot
- `probuild:client:<slug>:edit:<date>` — every edit request + resolution
- `probuild:client:<slug>:monthly:<ym>` — monthly performance report
- `probuild:action:<date>` — catalog log of every agent-completed action
- `probuild:self-review:<agent>:<date>` — per-agent self-critique
- `probuild:lesson:<topic>` — generalized lessons across clients
- `probuild:playbook:<procedure>` — repeatable SOPs
- `probuild:metric:<ym>` — monthly KPI snapshots
- `probuild:decision:<date>` — CEO-level decisions with rationale
- `probuild:spec-change:<agent>:<date>` — diff log of agent spec evolution
- `probuild:meta-review:<ym>` — Master Architect monthly cross-agent review

Rule: **every agent action produces at least one Brain entry.** Site shipped → `action` + `client:<slug>:launched` + `lesson` (from Lesson Extractor) + `self-review` (from each agent involved).

## Self-Improvement Loop (continuous, per agent)

Three nested cycles, every agent runs all three:

**Pre-task:** query Brain for last 5 similar tasks + self-reviews. Load learnings into context. Execute informed.

**Post-task:** 5-question self-critique (goal achieved? what worked? what to change? patterns? spec evolution?). Write `probuild:self-review:<agent>:<date>` memory.

**Weekly spec evolution (Sundays, per agent):** read the week's self-reviews, identify themes, propose spec updates. Minor changes auto-apply; major changes draft for CEO review. Updated spec replaces old one. Diff logged as `probuild:spec-change`.

**Monthly meta-reflection (Master Architect, cross-agent, 1st of month):** reads all agent self-reviews for the month, identifies cross-agent patterns, proposes system-level changes (new agents, merges, retirements). Writes `probuild:meta-review:<ym>`.

## Implementation Phases

- **Phase 0 — Brain prep** (~30 min): add `probuild` category to `memory.py`, update sphere UI hub, seed master index memory.
- **Phase 1 — Agent specs** (~1 hr): create `probuild-studio/.claude/agents/` directory, write Tier 1 custom agent files (7 total), mirror specs as Brain memories.
- **Phase 2 — Monorepo + factory** (~2 hrs): scaffold `C:\Users\User\probuild-studio\` monorepo, `pnpm new-site` CLI, build Hamilton demo plumber to validate pipeline.
- **Phase 3 — Action logging** (~30 min): `brain.logAction()` and `brain.logClientEvent()` utilities wired into CLI commands.
- **Phase 4 — Self-review loop** (~1 hr): extend `reflector.py` with `agent_self_review()`, add Sunday weekly cron + 1st-of-month monthly cron.
- **Phase 5 — First real client cycle** (month 2+): Sir cold-calls → closes → full automated pipeline fires → every step logs to Brain → monthly report writes itself.

## Agent Spec Format (canonical template)

Every agent lives in TWO places that stay in sync:
1. **`probuild-studio/.claude/agents/<agent-slug>.md`** — what Claude Code loads per session
2. **`probuild:agent-spec:<agent-slug>`** — Brain memory, source of truth, updated by self-review loop

Spec template (both locations):
- Name + slug
- Tier (1/2/3)
- Reports to (parent agent)
- Mission (one sentence)
- KPIs (2-4 measurable)
- Allowed tools (Claude Code tools the agent can use)
- Default model (usually Claude Opus for Tier 1, Sonnet for Tier 2, Haiku for Tier 3)
- Pre-task ritual (always queries Brain for prior self-reviews)
- Post-task ritual (always writes self-review to Brain)
- Current playbook (evolves weekly — pulled from most recent spec-change)

## Open Decisions (pending Sir greenlight)

1. New Brain category `probuild` added to `memory.py` vs reuse `nova` with tag prefixes. Recommended: new category.
2. Agent specs stored as both Markdown file + Brain memory (synced), or Brain-only. Recommended: both.

## Rules for Future Sessions

- Every ProBuild work session starts by querying the Brain for `probuild:master-architecture` + any `probuild:meta-review:<most-recent>`.
- Never create a ProBuild agent outside this hierarchy without updating the master architecture record.
- Every client interaction writes at least one `probuild:client:<slug>:...` memory.
- Every agent task writes one `probuild:action:<date>` entry AND one `probuild:self-review:<agent>:<date>` entry.
- Sunday 9am EST: weekly per-agent self-review cron runs.
- 1st of month 10am EST: Master Architect meta-review cron runs.
- Master Architect persona (this chat's tone) is the conversation default for ProBuild Studio work; it is NOT a separate custom agent.

## Connected Memories

- `2026-04-18-1930-northbuilt-brand-brief-locked.md` — SUPERSEDED (name changed to ProBuild Studio, tagline changed, global positioning). Keep for history only.
- `2026-04-18-1830-premium-websites-canada-reframe.md` — SUPERSEDED (pivoted to global scope via name change). Keep for history.
- `2026-04-18-1800-small-town-websites-startup-idea.md` — SUPERSEDED (abandoned volume play). Keep for history.
- Future: `probuild:brand-spec` (to be created under new `probuild` category with final locked name + tagline).
