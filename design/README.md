# Handoff: Loop — universal job application tracker

## Overview

Loop is a self-hosted application tracker that maintains itself. Instead of asking the user to log every application, it reads their mailbox and calendar, extracts applications and stage changes from that traffic, resolves the signals into one record per real job, and derives the pipeline. The user only intervenes for the ~1% of messages the system cannot place confidently.

Three surfaces: a mobile PWA (primary), a desktop dashboard for analysis and bulk work, and an onboarding flow whose entire purpose is to earn one mailbox permission.

Constraints the design assumes: self-hosted single-tenant but multi-tenant-ready, ~25 live applications, email + calendar as the only automated source (no portal scraping), manual add as the fallback, running cost target €0, English copy.

## About the design files

Everything in `design/` is a **design reference written in HTML**. These are prototypes that show intended look and behaviour — they are not production code to lift. They use a small in-house runtime (`support.js`) that renders `.dc.html` templates; that runtime is a prototyping tool, not part of what you are building.

**The task is to recreate these designs in the target codebase's environment**, using its established patterns and libraries. If no codebase exists yet, `design/Engineering Spec.dc.html` proposes a full stack (Node 22 + TypeScript + Fastify services, Postgres 16 with pgmq/pg_cron/pgvector, React 19 PWA) — treat that as a strong recommendation, not a mandate.

Open any `.dc.html` file directly in a browser to see it render. The prototypes are interactive: tabs, filters, sorting, row selection, drawers and sheets all work.

## Fidelity

**High-fidelity.** Final colors, typography, spacing and interaction states, all derived from the bound design system (Industry). Recreate pixel-perfectly using the codebase's own component library where equivalents exist; where they do not, the token values in §Design tokens below are exact.

Two caveats: all data in the prototypes is fabricated (Italian tech companies, plausible dates, invented ratios), and the mobile screens are drawn inside an iPhone bezel component that is scaffolding — the real target is a viewport-width PWA.

---

## Documents in this bundle

| File | What it is |
| --- | --- |
| `design/Engineering Spec.dc.html` | **Start here for implementation.** 19 sections, normative: DDL, event catalogue, queue contracts, connector behaviour, extraction ladder, resolver pseudocode with thresholds, metric SQL, API surface, security, GDPR, observability, test gates, milestones, open decisions. |
| `design/Architecture.dc.html` | The reasoning behind the system: why the mailbox and not the portals, service map, one message end to end, the extraction ladder, domain model, stage machine, metric definitions, privacy posture, cost model, build order. |
| `design/Architecture-print.dc.html` | Landscape print copy of the above. Content-identical; layout adjusted for paper. Ignore when implementing. |
| `design/Tracker Mobile.dc.html` | The mobile PWA: Today, Pipeline, Stats, application detail, plus review queue / quick-add / follow-up-draft sheets. |
| `design/Tracker Web.dc.html` | Desktop dashboard: dense table with filters, sort, multi-select, bulk archive; analytics rail; detail drawer. |
| `design/Tracker Onboarding.dc.html` | Seven-step connection flow, three empty states, four failure states. |

---

## Screens / Views

### A. Mobile — Today (`Tracker Mobile.dc.html`, tab `today`)

**Purpose.** The only screen a user needs on an ordinary day: what happened, what needs them, nothing else. It must read as calm when there is nothing to do.

**Layout.** Single column, 402×874 viewport (iPhone 16 logical size). Content area scrolls, padding `60px 18px 14px` (top clears the status bar). Fixed tab bar at the bottom, `border-top: 1px solid var(--color-divider)`, padding `var(--space-2) var(--space-2) 30px` (bottom clears the home indicator).

**Components, top to bottom.**

1. **Header row** — flex, `space-between`, `align-items: flex-start`, margin-bottom `var(--space-6)`.
   - Date eyebrow: Barlow Condensed 600, 11px, `letter-spacing .12em`, uppercase, `color-mix(in srgb, var(--color-text) 55%, transparent)`. Copy: "Thursday 30 July".
   - Headline: Barlow Condensed, 34px, `line-height 1`, uppercase, `letter-spacing -.01em`. Copy: "Three moved / forward / this week" (three explicit lines). **This headline is generated from the week's events** — it is the product's one encouraging gesture and must never be a static string. When the week had no forward movement, fall back to a neutral statement of fact ("Nine applications waiting"), never to false cheer.
   - Review-queue button: 44×44, `1px solid var(--color-divider)`, transparent, Lucide inbox icon at 18px stroke 1.5. Badge: 18px min-width, `background: var(--color-accent)`, `color: var(--color-bg)`, Barlow Condensed 600 11px, positioned `top:-7px right:-7px`, square. Hidden when the queue is empty.

2. **Counter strip** — 3-column grid inside `1px solid var(--color-divider)`, cells split by `border-right`. Each cell padding `var(--space-3)`: number in Barlow Condensed 600 30px `line-height 1`, label below in Barlow Condensed 600 10px `letter-spacing .11em` uppercase muted. Cells: "14 live" (default text), "4 interviewing" (`var(--color-accent-700)`), "1 offer" (cell background `var(--color-accent-100)`, number `var(--color-accent-900)`, label `var(--color-accent-800)`). The offer cell is the only tinted fill on the screen.

3. **Next up card** — full-width button, `.blueprint` class + four `<i class="corner tl|tr|bl|br">` marks, `background: var(--color-accent-100)`, padding `var(--space-4)`. Company in Barlow Condensed 600 22px `var(--color-accent-900)`; time right-aligned in Barlow Condensed 600 13px `letter-spacing .04em` uppercase `var(--color-accent-800)`. Subtitle 13.5px. Two chips below: "2 rounds done" (`1px solid var(--color-accent-400)`, `var(--color-accent-800)`) and a provenance chip "from calendar invite" (neutral border, muted text) — **provenance is shown on every automatically-derived claim**; it is what makes the automation trustworthy. Tapping opens the detail view.

4. **Suggestions** — section label "Suggested" (Barlow Condensed 600 11px `.12em` uppercase `var(--color-accent-700)`) with a right-aligned count. Max 3 cards, `1px solid var(--color-divider)`, padding `var(--space-4)`, gap `var(--space-3)`. Each: `.tag.tag-accent` kind label + right-aligned muted meta; title Barlow Condensed 19px `line-height 1.15`; body 13px `line-height 1.5` at 68% text; two buttons — `.btn.btn-primary` (flex 1, min-height 44px) and `.btn.btn-secondary` "Later" (min-height 44px). Dismissing removes the card from local state.
   - The four rules and their triggers are normative — see Engineering Spec §12.

5. **Recent activity** — label "Picked up this week", then rows: 56px min-height, flex, `border-bottom: 1px solid var(--color-divider)`, transparent background. Leading 11px `+` registration glyph (inline SVG, stroke `var(--color-accent)` at 1px, or muted for closed applications). Company 15px weight 500; description 12.5px at 60%; right-aligned day in Barlow Condensed 600 11px `.08em` uppercase at 45%.

6. **Closing line** — 12.5px at 55%: "Everything above was read from your mailbox and calendar. You have not typed anything this week." This sentence is the product's thesis; keep it or an equivalent.

**Tab bar.** Four items — Today, Pipeline, Stats, Add — each a flex column, min-height 44px, Lucide icon 20px stroke 1.5 over a Barlow Condensed 600 10.5px `.08em` uppercase label. Active `var(--color-accent)`; inactive `color-mix(in srgb, var(--color-text) 50%, transparent)`. "Add" opens a sheet rather than navigating.

### B. Mobile — Pipeline

**Purpose.** Every application in one thumb-scroll, grouped by phase.

**Layout.** Header block (padding `0 18px`) with eyebrow "Pipeline" and a 32px uppercase count headline. Below it a horizontally scrollable filter row (scrollbar hidden): chips at min-height 36px, padding `0 var(--space-4)`, `1px solid var(--color-divider)`, Barlow Condensed 600 12px `.06em` uppercase. Active chip: `background: var(--color-accent)`, `color: var(--color-bg)`.

**Group headers** — full-bleed band, `background: var(--color-accent-100)`, border top and bottom, padding `var(--space-2) 18px`, phase name left and count right, both Barlow Condensed 600 12px `.11em` uppercase `var(--color-accent-800)`.

**Rows** — full-width buttons, padding `var(--space-4) 18px`, `border-bottom: 1px solid var(--color-divider)`. Company Barlow Condensed 600 19px; stage right-aligned Barlow Condensed 600 11px `.07em` uppercase `var(--color-accent-700)`; role 13.5px at 70%. Meta line at 11.5px/50%: channel · 10px vertical divider · "quiet N days" · right-aligned flag in `var(--color-accent-700)` weight 500. Closed applications render at `opacity: .55` — dimmed, never hidden.

**Groups render in fixed order:** interviewing, screening, sent, decided. Empty groups are omitted entirely.

### C. Mobile — Stats

**Purpose.** Honest numbers. Every ratio shows its denominator.

- **Period toggle** — two-segment control in a 1px border, min-height 40px, active segment filled with the accent.
- **Funnel** — five stacked rows in one bordered box. Each row: label 13px/70% left, count Barlow Condensed 600 17px right, then an 8px bar — track `color-mix(in srgb, var(--color-text) 8%, transparent)`, fill `var(--color-accent)` at a width proportional to the top of the funnel.
- **Ratio cards** — label in Barlow Condensed 600 13px `.06em` uppercase at 70%, value in Barlow Condensed 600 30px `var(--color-accent-800)`, and **a mandatory note line at 11.5px/50% stating numerator, denominator and exclusions** ("11 of 68 · 5 too recent to count"). The note is not decoration: shipping the ratio without it is a bug.
- **Two-up figures** — median first human reply, ghost rate. 26px numerals, caption below.
- **Channel table** — `.table` class. Columns: Channel, Sent, → IV, → Offer, Ghost. The → IV column is the emphasis column (Barlow Condensed 600 14px `var(--color-accent-800)`). Referrals are always a separate row, never folded into a board.
- **Time in stage** — five rows, grid `118px | bar | 42px`. Bars 6px, fill `var(--color-accent-500)`.
- **Compensation** — `.blueprint` box with corner marks. Three tracks over a shared implicit scale: posted range (a `var(--color-accent-300)` span), the user's ask (a 2px `var(--color-text)` tick), offers received (2px `var(--color-accent)` ticks). Positions in the prototype are hard-coded percentages; implement against a real min/max domain.
- **Footer note** explains that seasonal statistics are withheld until there are two quarters of history. Below-threshold metrics show the count and name the gate — see Engineering Spec §11.

### D. Mobile — Application detail

Back bar (44px targets) with a right-aligned "tracked automatically" note. Company in Barlow Condensed 36px uppercase, role 15px/72%, then `.tag.tag-accent` stage and `.tag.tag-neutral` channel.

**Fact grid** — 2×2, cells divided by 1px borders: Applied, ATS, Location, Posted range. Labels Barlow Condensed 600 10px `.11em` uppercase at 50%, values 14px.

**Event log** — the core of the screen. Each entry is a grid `58px | 18px | 1fr`: right-aligned date in Barlow Condensed 600 12px `.04em` uppercase at 55%; a rail column with an 11px `+` mark over a 1px connector line in `var(--color-divider)`; then title 14.5px/500, detail 12.5px/65%, and a provenance row — a bordered chip naming the source ("gmail · template", "calendar · ics", "gmail · model", "quick add") plus "conf 0.94" in Barlow Condensed 600 10.5px `var(--color-accent-700)`. **Every automated event exposes its source and confidence.** This is the mechanism by which a user learns when to trust the system.

**Actions** — primary "Draft a follow-up" (48px), then a two-up row "Open thread" / "Correct stage". Footnote: correcting writes a `human_corrected` event at confidence 1.0 that the agent will not overwrite.

### E. Mobile — Sheets

- **Quick add** — bottom sheet over a `color-mix(in srgb, var(--color-neutral-900) 45%, transparent)` scrim. Posting-URL field first (metadata is fetched from it), then channel and date, then a 50px primary "Track it".
- **Review queue** — full-screen. Two card types: an *ambiguous match* (`.blueprint` with corner marks) offering candidate applications as 48px-tall selectable rows plus a "Neither — new application" escape; and an *unknown template* card with a yes/no pair. Both quote the source message in a 12.5px excerpt with a 2px `var(--color-accent-300)` left rule. Copy states that answers are written back as rules so the queue shrinks over time.
- **Follow-up draft** — bottom sheet. Rendered draft in a bordered box at 14px/1.6. Footnote states Loop holds a read-only scope and cannot send. Actions: "Copy & open thread" and "Edit". **There must be no send path anywhere in the implementation.**

### F. Web dashboard (`Tracker Web.dc.html`)

1440px reference width; the layout is fluid with a fixed 400px right rail.

- **Top bar** — brand, four nav items (Barlow Condensed 600 12px `.09em` uppercase; the active one carries a 2px accent underline), then a right-aligned health line: a 7px accent square + "Gmail + Calendar connected · last read 4 min ago · 3 messages placed today". **This health string is load-bearing** — a silent connector is indistinguishable from a quiet job market, so freshness is always on screen.
- **KPI strip** — six cells divided by 1px borders, 34px numerals over 10px `.11em` uppercase captions. The "open offer" cell carries the `var(--color-accent-100)` fill.
- **Table** — `.table` class. Columns: 34px checkbox, Company, Role, Stage, Channel, Applied (right), Last signal (right), Flag. Filter chips and three sort buttons sit in a bar above it. Checkboxes are 15px squares — filled `var(--color-accent)` when selected. "Last signal" turns `var(--color-accent-700)` past 13 days. Closed rows at `opacity: .55`.
- **Bulk bar** — appears only with a selection: `var(--color-accent-100)` band with count, "Archive as dormant" (primary), "Set stage…", "Export CSV", and a right-aligned "Clear".
- **Right rail** — period toggle, funnel (9px bars), two ratio cards with their denominators, channel table, time-in-stage bars. Note under the channel table: referrals are reported separately on purpose.
- **Detail drawer** — 520px, right-anchored over a 35% scrim, `border-left: 1px solid var(--color-divider)`. Same content as the mobile detail view; actions become a four-button row.

### G. Onboarding (`Tracker Onboarding.dc.html`)

Seven steps. The ordering is the design: **explanation precedes consent, and confirmation precedes any statistic.**

1. **Welcome** — 40px uppercase headline "You already / sent them", three `+`-marked value lines. CTA "Connect a mailbox", sub-CTA "or add applications by hand".
2. **What Loop reads** — two stacked bordered boxes, "It can" (header band `var(--color-accent-100)`) and "It cannot". The cannot-box carries equal weight: read-only scope, no body storage, no access beyond mail and calendar. Scopes named literally (`gmail.readonly`, `calendar.readonly`). This screen must not be skippable.
3. **Google consent** — a representation of the real system screen inside a `.blueprint` frame, including the unverified-app warning, explained *before* the user meets it.
4. **History depth** — three options (3 months / 12 months / everything) with time estimates. Selected row tinted `var(--color-accent-100)`.
5. **First scan** — a 9px progress bar, a date range, a three-cell counter (read / found / unsure) with the "found" cell tinted, and a live-updating list of what it is finding with provenance chips. Copy note: showing findings instead of a spinner is the cheapest trust the product can buy.
6. **Confirm the haul** — checkbox list of found applications; ambiguous ones carry an accent-bordered "review" chip. The user approves before this becomes their pipeline.
7. **Notifications** — the four permitted interruption reasons listed, then a `var(--color-accent-100)` box stating the fixed rules: one notification a day, nothing 21:00–08:00, never twice for the same thing, rejections never pushed.

Footer: a 7-segment progress tick row (3px, filled to current step), a back/next button pair at 50px, and a sub-CTA line.

### H. Empty states

- **E1 — Day one, nothing found.** Headline "Nothing / to track yet". A bordered status box with a 7px accent square, "Watching your mailbox", and text explaining that new mail appears on its own. Primary action is manual add. Footer: "Scanned 2 418 messages · 0 about a job. That is normal for a first day." **Empty must read as working, not broken.**
- **E2 — A quiet day.** "You are clear today", the counter strip with `0 overdue`, an explanation of what was checked, and the next calendar item. Footer: "A tracker that always has a task for you is inventing tasks. This screen is a feature."
- **E3 — Not enough data.** "Too early / to mean / anything". Raw counts instead of ratios, then a `var(--color-accent-100)` box naming each threshold (ratios at 8 closed, time-in-stage at 5 transitions, seasonal at 2 quarters). Naming the gate turns an empty chart into a progress bar.

### I. Failure states

Every failure states **when the data was last trustworthy** and **whether anything was lost**.

- **F1 — Access revoked.** The only full-screen failure, because it is the only one the system cannot fix alone. Accent-bordered banner with a Lucide alert-triangle: "Loop stopped reading 4 days ago". Body reassures nothing was lost and that missed mail will be caught up. A three-row fact box: last successful read, applications kept, estimated missed messages. Actions: "Reconnect Google", "Use a different mailbox".
- **F2 — Watch lapsed.** Degraded, not broken. A thin status strip "Catching up · 41 messages behind" above an otherwise normal Today screen; a progress bar and "nothing needs doing". Recently-found items render at `opacity: .55` while the backlog drains. No dialog, no action.
- **F3 — IMAP login refused.** Shows the verbatim server response in a bordered box, then two probable causes (2FA needs an app-specific password; IMAP disabled in webmail). Footnote: retries stop after two attempts so the provider does not lock the account.
- **F4 — Model offline.** Per-component status list: template rules "running", calendar detection "running", local model "unreachable · 22 min" (tinted row). Explains that 78% of mail is unaffected and the queue drains itself. Offers manual review of the parked items. **Partial failure is named per component so a stalled model never reads as "the app is broken".**

---

## Interactions & behavior

| Trigger | Result |
| --- | --- |
| Tab bar item | Switches the mobile view; clears any open detail or sheet |
| "Add" tab | Opens the quick-add bottom sheet (does not navigate) |
| Review badge | Opens the full-screen review queue |
| Next-up card / recent row / pipeline row | Opens the application detail |
| Detail back button | Returns to the previous tab, scroll position preserved |
| Suggestion primary | Follow-up → draft sheet; deadline → the application; archive → bulk action |
| Suggestion "Later" | Removes the card for the session; the rule re-triggers per §12 |
| Filter chip / sort button (web) | Re-filters and re-sorts client-side; single active value each |
| Row checkbox (web) | Toggles selection; the bulk bar appears at ≥1 selected |
| "Archive as dormant" | Removes the rows from the list, keeps them in statistics as ghosted |
| Onboarding step list | Jumps directly to that step (prototype affordance; the real flow is linear) |
| Haul checkbox | Toggles inclusion before commit |

**Transitions.** Sheets slide up from the bottom; the web drawer slides in from the right; both over their scrims. Keep them under 200ms — this is a utility, not a showcase. Everything else is an immediate state swap.

**Live updates.** The clients hold an SSE connection (`GET /api/stream`) and apply `application.changed`, `scan.progress`, `review.added` and `mailbox.status` in place. A stage change arriving while the user is looking at the list should animate the row, not reload the view.

**Responsive.** The mobile design is the PWA at any phone width — the bezel is scaffolding. The web dashboard collapses the right rail below the table under ~1100px; below ~700px it should defer to the mobile layout rather than compress further.

**States to build that the prototypes only imply:** per-row loading skeletons during first paint, optimistic archive with undo, offline read-only mode (service worker cache), and a toast for failed mutations.

---

## State management

**Client, ephemeral:** active tab, open application id, open sheet, selected rows, filter, sort, stats period, dismissed suggestions, onboarding step.

**Server, authoritative:** everything else. The client never computes a statistic, never derives a stage, and never decides whether an application is dormant — all of that arrives precomputed, including `days_quiet` and each row's `flag`. See Engineering Spec §11 and §13.

**Data fetching:** `GET /api/today` for the daily view (one round trip: counters, next interview, suggestions, recent events, mailbox health), `GET /api/applications` cursor-paginated for the pipeline, `GET /api/stats?period=` for statistics, `GET /api/review` for the queue. Cache per query key, invalidate on the matching SSE event.

---

## Design tokens

All values come from the **Industry** design system (`design/_ds/industry-*/styles.css`), which is included in this bundle. Use its `var(--*)` tokens rather than the literals below; the literals are given so you can map them onto an existing system.

**Color** — ground `--color-bg` #f2f2f3 · text `--color-text` #1d1f20 · accent `--color-accent` #5980a6 · `--color-divider` for every hairline. Ramps run 100–900 on a shared perceptual scale: `--color-accent-100` for tinted fills, `-300`/`-400` for light marks and borders, `-500` base, `-700`/`-800`/`-900` for text on tint and for emphasis. Muted text is `color-mix(in srgb, var(--color-text) N%, transparent)` at 50/55/60/65/68/70/72/75 — the ladder is deliberate: 70–75% for body, 55–60% for captions, 45–50% for footnotes.

**Type** — headings Barlow Condensed (`--font-heading`), body Barlow (`--font-body`). Observed scale: display 54–68px, section 26–38px, screen title 30–40px, card title 19–22px, body 13–15.5px, caption 11.5–12.5px, eyebrow 10–11px at `letter-spacing .11–.12em` uppercase. Headings are weight 600 and stay condensed; body stays 400/500.

**Spacing** — `--space-1` … `--space-8` (density 0.85×). Never hard-code px for layout gaps.

**Borders and radius** — the system is square-cornered. `1px solid var(--color-divider)` is the universal edge. Cards and figures are transparent line drawings; the primary button is the one solid accent fill. Framed objects carry `.blueprint` plus four `<i class="corner tl|tr|bl|br">` registration marks — **do not drop the marks from a framed element**.

**Elevation** — `--shadow-sm/md/lg` only, and sparingly. This design uses borders, not shadows.

**Focus** — `:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }`. Never leave the browser default.

**Touch targets** — 44px minimum on mobile, enforced throughout.

---

## Assets

No images, photographs or raster assets. Icons are **Lucide** at stroke-width 1.5, inlined as SVG in the prototypes — install `lucide-react` (or the equivalent) rather than copying the paths. The `+` registration glyph is a two-path SVG at 11px, stroke 1.

`design/ios-frame.jsx` and `design/doc-page.js` are prototyping scaffolds (device bezel, printable page shell). Do not port them.

---

## Files

```
design/
  Engineering Spec.dc.html        ← normative implementation spec, read first
  Architecture.dc.html            ← design rationale and system map
  Architecture-print.dc.html      ← print copy of the above
  Tracker Mobile.dc.html          ← mobile PWA
  Tracker Web.dc.html             ← desktop dashboard
  Tracker Onboarding.dc.html      ← onboarding, empty states, failure states
  support.js                      ← prototype runtime (not for production)
  ios-frame.jsx                   ← device bezel scaffold (not for production)
  doc-page.js                     ← print shell scaffold (not for production)
  _ds/industry-*/styles.css       ← design tokens and component classes
  _ds/industry-*/readme.md        ← design system guide
```

---

## Before you start: six open decisions

These are unresolved in the design and at least one of them changes the data model. Confirm with the product owner rather than guessing — Engineering Spec §19 has the full framing.

1. Which mail providers are actually in use — if everything is Gmail, IMAP moves to a later phase.
2. May a take-home deadline alert break quiet hours? (Spec currently says yes; it is the only exception.)
3. Do dormant applications stay in the funnel denominator? (Spec says yes. Changing this changes every ratio.)
4. Timezone source: device or user setting — affects the 03:00 dormancy job and the 21:00 boundary.
5. Auto-merge at ≥0.93 cosine, or always ask? (Spec auto-merges outside listed exclusions.)
6. Retention of resolved review excerpts.

## Build order

Phase 1 is the project's go/no-go and it is not the UI: build the connector, classifier, ten ATS rules and resolver, backfill twelve months of a real inbox, and measure. Target ≥0.85 application-level recall with zero wrong merges. If extraction is not accurate enough to trust, no amount of interface work saves the product. Milestones and acceptance criteria are in Engineering Spec §18.
