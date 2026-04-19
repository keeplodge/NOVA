# NorthBuilt Web Creations — Brand Brief (Locked 2026-04-18)

Canonical brand brief for NorthBuilt Web Creations, a Canadian-trades-focused website agency. Locked in this session and load-bearing for all future work on this business. Do not drift these specs without Sir's explicit sign-off. Business concept: bespoke-designed websites for Canadian trades contractors (starting with plumbers, GTA-first, Hamilton as pilot city #1) at $499 setup + $99/mo. Uses Claude Design + Claude Code pipeline to collapse the design+dev labor cost that traditional agencies can't match.

## Locked Brand Spec

- **Agency name:** NorthBuilt Web Creations
- **Primary tagline:** "Websites Built For Trades. Built In Canada."
- **Ad / landing-page headline (secondary):** "The Website Your Next Job Lives On."
- **Positioning statement (canonical):** "NorthBuilt is the website company for Canadian trades — bespoke design, built-in SEO, Google-first performance — delivered in 5 days for the cost of one weekend callout."
- **Visual direction:** Industrial Blueprint (Carhartt × architect's office). Navy-dominant with orange CTAs, blueprint line-art decoration, slab/geometric-sans headlines.
- **Domain status:** NOT yet registered (deferred until demo-ready). Target when registering: `northbuiltweb.ca`.

## Color palette (hex locked)

- Blueprint Navy `#0A1929` — primary backgrounds, headline text
- Steel Grey `#4A5568` — secondary text, borders, muted UI
- Signal Orange `#FF6B00` — **CTAs ONLY**, never decorative
- Neutral Light `#F8FAFC` — body backgrounds
- Success Green `#00C853` — form success only
- Warning Red `#E53E3E` — form errors only
- White `#FFFFFF` — cards, inverse text on navy

**Rule:** Signal Orange is reserved for buttons, emergency banners, and interactive hover states. If orange appears as a decorative element, the brand is being diluted. Enforce this on every site shipped under the NorthBuilt umbrella.

## Typography (locked)

- Display / hero headlines: **Archivo Black** (Google Fonts, free)
- Secondary headlines: **Space Grotesk 700** (Google Fonts, free)
- Body text: **Inter 400 / 500 / 600** (Google Fonts, free)
- Technical / overline / schema labels: **JetBrains Mono 500** (Google Fonts, free)

All four load as a single Google Fonts subset for perf. No paid licenses.

## Voice rules

- Direct — never "Hello there!" or "We're excited to..."
- Authoritative — never apologetic about timelines or scope.
- Plain — no jargon (no "synergy," "leverage," "solutions").
- Outcome-focused — "books jobs," "brings leads," "ranks you" — never "modern design" or "beautiful aesthetic."
- Tradesman peer voice — write as a craftsman who runs an agency, not an agency explaining itself to a tradesman.

## First business target

- **Vertical:** Plumbers.
- **First city (pilot + demo #1):** Hamilton, Ontario.
- **Price point:** $499 setup + $99/mo hosted & monitored. 90-day lead guarantee (refund setup if no lead from the site).
- **Expansion sequence:** Brampton → Mississauga → Vaughan/Markham → Oakville/Burlington → Kitchener-Waterloo → London → Barrie → Ottawa → Windsor. Phase 3 (month 4+): Calgary/Edmonton/Vancouver. See `2026-04-18-1830-premium-websites-canada-reframe.md` and ICP session notes for full Canada map.

## Non-negotiable quality gates (every site, no exceptions)

- Lighthouse Performance ≥ 90 (mobile)
- Lighthouse Accessibility ≥ 95
- Lighthouse SEO = 100
- Lighthouse Best Practices ≥ 95
- axe-core critical issues = 0
- LocalBusiness + Plumber schema validated
- Real content (no Lorem Ipsum, no placeholder imagery on live sites)
- Emergency CTA (phone + book-now) visible in <1 scroll on 360px mobile
- GMB profile link confirmed before site goes live

## How this connects to existing Sir projects

- **KeepLodge** — different business, different customer, different shape. Do NOT port KeepLodge's template engine here. Bespoke-per-client is the wedge; template engines kill it.
- **SEO skill stack** (seo-audit, seo-local, seo-schema, seo-content, seo-page) bakes directly into every NorthBuilt site as a free differentiator.
- **Claude Design (launched 2026-04-17)** handles per-client visual work that would otherwise require a design team.
- **Claude Code** handles production build from Claude Design handoff bundle.

## For future sessions

- This brief supersedes earlier iterations (volume-play at $300-$1K small-towns-only AND the $3-15K premium reframe). Both are abandoned directions.
- Do not re-debate the brand name, tagline, positioning, palette, or type. Those are locked. If Sir wants to change one, he'll say so explicitly.
- Next work sessions should pick up at: Phase 1 scaffolding (NorthBuilt agency site + Hamilton plumber demo #1 in Next.js 14 App Router + Tailwind + shadcn/ui + Vercel).
