# ProBuild Studio — Project Status Snapshot (2026-04-19)

End-of-session canonical status for ProBuild Studio after a full build arc: monorepo scaffolding, Hamilton plumber demo, agency site with competitive positioning, Claude Design handoff implementation, and site-wide fix pass resolving every broken reference from the token-system migration. Five pages are live on localhost, GitHub repo pushed through commit 07a7c94, 54 probuild-tagged memories in the Brain, and a full status bundle saved to Sir's Desktop at ProBuild-Studio-Status/. This memory is the single-source-of-truth status for where the build stands; future sessions should query this first before touching code.

## Where everything physically lives

- Monorepo: C:\Users\User\probuild-studio\
- GitHub: https://github.com/keeplodge/ProBuild-Studios (private, keeplodge org)
- Agency site dev server: http://localhost:3000 (apps/agency-site, pnpm dev --filter=agency-site)
- Hamilton plumber demo dev server: http://localhost:3001 (apps/demos/hamilton-plumber-1)
- Claude Design handoff bundle: C:\Users\User\Downloads\ProBuild Studio Design System-handoff.zip (extracted at Downloads\probuild-handoff\)
- Tier 1 agent specs: probuild-studio\.claude\agents\ (7 files)
- Desktop reference bundle: C:\Users\User\Desktop\ProBuild-Studio-Status\ with README.md + screenshots/ (01-home through 05-compare)

## Locked decisions (do not drift without Sir explicit sign-off)

- Name: ProBuild Studio
- Tagline (sentence case): "the website your next job lives on."
- Location: **Brantford, ON** (overrode the handoff's Calgary default per Sir 2026-04-19)
- Phone placeholder: (519) 555-0131 — Brantford area code, replace before launch
- Email placeholder: hello@probuild.studio — replace before launch
- Domain target: probuildstudio.ca (not yet registered)
- Pricing (hardcoded in PricingGrid.tsx):
  - Core: $499 + $99/mo → $1,687 year-1
  - Plus (most popular): $499 + $149/mo → $2,287 year-1
  - Pro: $499 + $249/mo → $3,487 year-1
  - Enterprise: $999 + $499/mo → $6,987 year-1
- All CAD. Triple-Backed 90-Day Guarantee on every tier.
- Brand palette: Blueprint Navy #0A1929, Navy 2 #0F2238, Steel Grey #4A5568, Signal Orange #FF6B00 (CTAs ONLY), Neutral Light #F8FAFC.
- Fonts: Archivo Black (display), Space Grotesk (headings), Inter (body), JetBrains Mono (overline/technical) — all free Google Fonts.
- Golden Rule: Signal Orange only on CTAs, hover states, and the Most Popular pricing ribbon. If orange shows decoratively, the brand is broken.

## Live pages (all consistent with design system after 07a7c94)

- / (Homepage) — Nav + Hero (asymmetric 12-col with spec sheet card) + TrustStrip + FeatureGrid (6 cards, 3x2 asymmetric) + PricingGrid + FAQ + Footer
- /pricing — dedicated pricing hero + PricingGrid + FAQ
- /case-studies — Steel City Plumbing as flagship demo, single-case layout with 2x2 metric grid, founding-client CTA
- /about — article layout explaining "Option C" positioning, Brantford bake
- /compare/[slug] — dynamic SEO-moat pages for 7 competitors (footbridge-media, blue-collar-marketing, platinum-design, hook-agency, blue-corona, wix-squarespace-diy, fiverr-freelancers); each shows price comparison with orange savings banner, strengths/weaknesses cards, Why-ProBuild-wins navy block

All pages use the --pb-* CSS variable token system. All share SiteShell which wraps children with Nav + Footer + QuoteModal via React context (useQuoteModal hook). Mobile hamburger drawer works.

## Components (13 live)

Icon, Button, Nav, Hero, TrustStrip, FeatureGrid, PricingGrid, FAQ, Footer, QuoteModal (all 10 ported from Claude Design handoff) plus SiteShell (glue wrapper with modal context) plus InlineQuoteCTA (shared CTA block used on compare/case-studies/about pages).

## What's intentionally NOT done yet (flagged for next session)

- TrustStrip uses placeholder text logos ('CHBA', 'Roofing CA', etc.) — real partner/trade-association marks needed before launch
- QuoteModal console.log's instead of POSTing to a real /api/quote endpoint — production needs Resend wiring with RESEND_API_KEY env var
- Legal pages (Terms, Privacy, Accessibility) are placeholder # links — real content required before public launch
- Domain probuildstudio.ca not yet registered
- No Vercel deployment yet — the site exists only on localhost; production deploy is next big move
- No /api/quote backend — form submissions only log to console
- Hero uses CSS blueprint grid placeholder — real job-site photography flagged as needed before launch
- CLAUDE.md at probuild-studio root still references the pre-handoff brand position and should be updated to match final locked state (minor drift, not breaking)

## Tech stack (canonical)

- Next.js 15.0.3 App Router + React 19 RC + TypeScript strict
- Tailwind CSS v4 (@theme directive + --pb-* CSS variables loaded from globals.css)
- pnpm 9.12.0 workspaces + Turborepo 2.9.6 monorepo
- Biome 1.9.4 (lint + format, replaces ESLint + Prettier)
- next/font/google for all four typefaces (zero layout shift)
- Icons: inline SVG from 15-icon Lucide subset (Icon.tsx); currentColor inheritance
- Deploy target: Vercel (free tier per project, Canadian edge)

## Rules for future sessions

- Pricing, brand name, tagline, palette, typography are LOCKED. Do not rewrite any of these without Sir's explicit sign-off.
- Signal Orange appears only on CTAs, CTA hover, and the "Most Popular" pricing badge. Never decorative. Violating this breaks the brand.
- Voice rules from SKILL.md are non-negotiable: no emoji, no unicode flair (checkmarks / arrows / dots in body copy), no "We're excited to…", no "leverage", no apologetic language about price or timeline.
- Every new component MUST use --pb-* CSS variables, never hex literals. If you're typing `#FF6B00`, stop and use `var(--color-pb-orange)`.
- Every new page MUST use pb-container for max-width, and the pb-overline → pb-h1/h2-display → body → CTA pattern.
- Components live in apps/agency-site/src/components/. Pages in apps/agency-site/src/app/. Content (locked data) in apps/agency-site/src/content/ (only competitors.ts remains after cleanup).
- Always screenshot-verify via CDP before saying "done" — hot-reload alone doesn't prove visual correctness.

## Commit history (last 5 canonical)

- 07a7c94 — Site-wide fixes (inner pages restyled, mobile nav, real footer links)
- 26bb87c — Claude Design handoff implementation (agency-site rebuild on design system)
- 3efabee — First-pass agency site with competitive positioning
- 4eb003f — Hamilton plumber demo #1
- 8e7c43f — Initial monorepo scaffold

## Next moves (priority-ordered)

1. Sir cold-calls a Brantford plumber/roofer/electrician to land client #1 — the Hamilton demo + agency site are the pitch kit ready for an iPad at a Rona PRO counter
2. Register probuildstudio.ca, deploy to Vercel production, swap localhost URLs for real domain
3. Wire /api/quote backend endpoint via Resend so QuoteModal submissions reach hello@probuild.studio
4. Replace TrustStrip placeholders with real partner logos once Sir joins CHBA / relevant trade associations
5. Draft real Terms / Privacy / Accessibility pages (Canadian legal review recommended)
6. Once first real client ships, fork apps/demos/hamilton-plumber-1 template to apps/clients/<slug>/ and begin client #1 build; update case-studies page with real metrics replacing the demo

## Cross-references in the Brain

- probuild:master-architecture — agent org chart, autonomous agency design, Brain namespace
- probuild:brand-spec — canonical brand doc (ProBuild Studio header)
- probuild:competitive-analysis-v1 — 10 dominance moves, competitor matrix
- probuild:client:hamilton-plumber-1:build-log — demo #1 record
- probuild:design-system-handoff-implemented — Claude Design bundle integration log
- probuild:agent-spec:<slug> — 7 Tier 1 agent definitions
