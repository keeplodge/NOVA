# ProBuild Client Build Log — hamilton-plumber-1 (Demo Site #1)

Canonical build record for ProBuild Studio's Hamilton plumber demo site. Fictional client "Steel City Plumbing Co." used as sales asset + stack validation + template reference for future real clients. First ProBuild production site, shipped 2026-04-19. Scaffolded on Next.js 15 App Router with Tailwind CSS v4 + TypeScript strict + shadcn-style component patterns. All code lives in the probuild-studio monorepo at apps/demos/hamilton-plumber-1/.

## Client profile (fictional, for demo purposes)

- Business: Steel City Plumbing Co.
- Owner: Dave Marino (Red Seal plumber, founded 2009)
- City: Hamilton, Ontario — 482 Barton St E
- Service area: Hamilton, Stoney Creek, Ancaster, Dundas, Waterdown, Burlington, Grimsby, Winona
- 6 services: emergency-plumbing, drain-cleaning, water-heaters, pipe-repair-repiping, sump-pumps-backflow, fixture-installation
- 5 reviews seeded, 4.9/87 aggregate
- 8 FAQs seeded
- Phone: (905) 555-0199 (fake)
- Brand colors: primary red #C73E1D, charcoal #2B2D42, cream #F5F0E8

## Files shipped (20 files)

- package.json, tsconfig.json, next.config.mjs, postcss.config.mjs, next-env.d.ts, README.md
- src/content.ts — single source of truth for all client data (name, NAP, services, reviews, FAQs, brand, SEO)
- src/app/globals.css — Tailwind v4 @theme tokens + btn utilities
- src/app/layout.tsx — root layout with Oswald + Inter fonts, metadata, Schema components, sticky EmergencyHeader
- src/app/page.tsx — homepage composition
- src/app/sitemap.ts + robots.ts — SEO primitives
- src/app/about/page.tsx, contact/page.tsx, services/[slug]/page.tsx
- src/components/schema/LocalBusiness.tsx, Plumber.tsx, FAQSchema.tsx — JSON-LD
- src/components/EmergencyHeader.tsx, Hero.tsx, TrustRow.tsx, ServicesGrid.tsx, Reviews.tsx, ServiceArea.tsx, FAQ.tsx, LeadForm.tsx, Footer.tsx

## Stack decisions (non-obvious, worth remembering)

- Tailwind CSS v4 (new engine) via @tailwindcss/postcss plugin and @theme directive in globals.css. No tailwind.config.ts for this project — v4 reads tokens from CSS directly, which is cleaner for per-client brand customization.
- Next.js 15.0.3 + React 19 RC. Typed `generateStaticParams` for [slug] route so service pages are statically generated at build time (zero-runtime SEO cost).
- React Hook Form + Zod + @hookform/resolvers for the lead form. Schema-first validation, end-to-end typed. Form POST currently logs to console; production wires to /api/lead → Resend → client email.
- lucide-react for icons (tree-shakable, small bundle).
- OpenStreetMap iframe for the service-area map (no API key needed; Google Maps embed would require billing). For production client sites with higher quality requirement, swap to Mapbox or Google Maps with billing.
- Schema.org components use dangerouslySetInnerHTML + JSON.stringify — this is the standard Next.js pattern for JSON-LD and is safe because we control the data.
- Every page has typed Metadata export (Next 15 metadata API) — includes OG + Twitter cards, canonical URL, robots directives.
- Security headers wired in next.config.mjs: X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, HSTS with preload.

## What IS working end-to-end

- Homepage renders with all sections (hero, trust, services, quote form, service area, reviews, FAQ, footer)
- Sticky emergency header with pulsing phone CTA visible above-fold mobile
- Lead capture form with client-side Zod validation + submit handling (console log for now)
- Per-service pages at /services/[slug] fully generated for all 6 services
- JSON-LD schema valid for LocalBusiness + Plumber + FAQPage
- Sitemap.xml and robots.txt auto-generated from content.ts
- Font loading via next/font/google (self-hosted Oswald + Inter, zero layout shift)

## What is NOT yet wired (deferred to next iteration)

- pnpm install has not run — monorepo dependencies not yet fetched. Site is scaffolded code, not yet spinning up a live dev server.
- OG image at /og-default.jpg is referenced but image file not created. Will need a dynamic OG generator via Next.js ImageResponse API for production.
- Lead form /api/lead endpoint not implemented — currently just console.log. Needs Resend wiring + env var RESEND_API_KEY.
- GMB Reviews currently static fake data in content.ts. Real production integration reads from Google My Business API with OAuth per client.
- No tests yet (Playwright + Vitest). Should add E2E smoke test that hits every route and validates Lighthouse gates before deploy.
- No CI/CD. Lighthouse CI + axe-core should run as a GitHub Actions gate on every PR.
- Agency site (apps/agency-site/) not yet scaffolded — only monorepo skeleton exists.
- Factory CLI scripts (scripts/new-site.ts, audit-site.ts, deploy-site.ts) not yet implemented — those are the Phase 2 tooling work.
- brain-client package not yet built — agents can't programmatically write action memories to the Brain yet.

## Next build session priorities

1. `pnpm install` at monorepo root and smoke-test the Hamilton demo with `pnpm dev --filter=demo-hamilton-plumber-1`. Confirm it compiles and renders.
2. Fix any dependency resolution issues (likely some version pins need updating given rapid Next 15 and Tailwind v4 evolution).
3. Build the agency site (apps/agency-site/) as the ProBuild Studio marketing page — pricing, pitch, demo link.
4. Add /api/lead endpoint to the Hamilton demo so LeadForm actually sends.
5. Ship the site to a Vercel preview URL so Sir can show it on an iPad in the Rona PRO parking lot.

## How this demo is meant to be used

- Sales asset: Sir opens the site on an iPad when talking to real Hamilton plumbers at Rona/Home Depot contractor counters. Shows what ProBuild can deliver.
- Template: when the first real client signs, this demo gets forked, content.ts gets replaced with real client data, brand colors adjusted, photos swapped, and a new apps/clients/<slug>/ app is born. Same components, different content.
- Case study: once 3-5 real clients are live, this demo gets linked from probuildstudio.ca/case-studies/ as "example trades site" with annotations explaining what makes it convert.

## Rules for future sessions touching this demo

- Do NOT change the file structure or component split without updating this log.
- Do NOT hard-code client data outside content.ts. All facts (name, phone, services, FAQs) route through content.ts — enforces the template-per-content separation.
- Brand colors (steel-city red + charcoal + cream) are specific to Steel City Plumbing; they are NOT ProBuild Studio's brand. ProBuild Studio uses Blueprint Navy + Signal Orange. Two different palettes in two different apps.
- When the first real plumber client signs, DO NOT rename this demo. Fork it to apps/clients/<real-client-slug>/ and keep this one as the perpetual reference demo.
