# Loop — decision register

Companion to `design_handoff_loop_tracker/`. The Engineering Spec is normative; where this
document contradicts it, this document states why and wins, because the spec as written either
does not compile, does not run, or contradicts another normative sentence in the same bundle.

Status legend: **RESOLVED** — decided here, implement it. **OPEN** — needs the product owner.

---

## A. Contradictions between normative documents

### A1 — The fold precedence rule freezes every application. RESOLVED (spec amended)

Spec §05: *"For each field, take the event with the highest `confidence`; ties break by latest
`occurred_at`, then by highest `id`."*

Architecture §03 walks one message end to end: an `acknowledged` event at confidence **0.99**
(ATS auto-reply, day 0), then eleven days later `stage_advanced → hr_screen` at confidence
**0.94**. Under the literal §05 rule 0.99 outranks 0.94, so the fold keeps the stage at
`acknowledged` forever. Every ATS auto-reply is a 0.99 template match and almost every human
signal is scored lower, so the literal rule makes the pipeline structurally incapable of
advancing — the exact scenario the architecture uses as its worked example.

Architecture §06 states the intent differently and correctly: *"The fold takes the
highest-confidence **latest** event."*

**Amended rule.** Per field, order candidate events by:

```
(pinned desc, occurred_at desc, confidence desc, id desc)
    where pinned = confidence >= 1.0        -- human_corrected and user actions
```

plus a floor: events with `confidence < REVIEW_BELOW (0.60)` never participate in the fold —
they exist in the log and go to the review queue, which is what the ladder's rung 4 already
promises. This preserves both stated intents ("your correction always wins", "the latest
signal moves the stage"), stays a pure deterministic function of the event set, and keeps the
property test — replay in any arrival order yields the same state — valid.

Confidence is not discarded: it still decides *whether* an event is written at all, it is
displayed on every event row, and it breaks ties between two events at the same instant.

**A1b — the tie-break may not be `id`.** §05 ends ties with "then by highest id". `id` is a
bigserial, i.e. *arrival* order, so a fold that consults it depends on the order messages
happened to be delivered in — and §17's property test ("replaying events in any arrival order
yields the same state") could not pass. The tie-break here is content-derived
(`evidence_ref` + type), which is stable across replays. A test asserts it by folding the same
set with reversed ids.

**A1c — what "pinned" means.** §05 says a human correction carries confidence 1.0 and pins its
field, which reads as "confidence 1.0 pins". It cannot mean that: quick add also writes at 1.0,
and if that pinned the stage then every hand-added application would be frozen at `applied`
forever, deaf to its own mailbox. Pinning is therefore about authorship and scope — only a
`human_corrected` event pins, and only the field it names.

### A2 — The closed event set is 13 types, but the two documents list different 13. RESOLVED

Spec §05 has `deadline_set` and no `note_added`. Architecture §05 has `note_added` and no
`deadline_set`. The `prepare` nudge (Architecture §08, Spec §12) promises to surface *"your
notes on this company"*, which requires notes to exist.

**Set is 14**: the Spec's 13 plus `note_added` — payload `{text}`, confidence 1.0, `rung` null,
no effect on stage or status, excluded from every metric spine (it is not a signal about the
world, it is the user talking to themselves). Adding it is a migration and a change to the
fold, per the spec's own rule about the closed set.

### A3 — The notifier sends a weekly digest email, and the repo may contain no send path. RESOLVED

Spec §02/§03: `notifier/ web push (VAPID), weekly digest email`.
Spec §12: *"there is no code that calls an SMTP or Gmail send API anywhere in the repo, and a
CI grep asserts it."*

These cannot both hold. The absolute version is the more valuable one — it is the promise that
makes a read-only mailbox scope credible.

**v1 digest is not email.** It is a rendered page at `/digest/:isoWeek` plus one web-push
notification linking to it. No SMTP client, no `nodemailer`, no transport dependency of any
kind. The CI grep stays unconditional:

```
smtp|nodemailer|sendmail|createTransport|messages\.send|users\.messages\.send
```

If email delivery is ever wanted it becomes a separate opt-in container with its own
credentials and the grep is narrowed by an explicit, reviewable exception — never silently.

### A4 — "No build step for the client" vs. Vite + React 19 + a CSP without `unsafe-inline`. RESOLVED

Read as *no SSR runtime to operate*. The client is built with Vite and served as static files
by the gateway behind Caddy. A strict CSP is only possible **because** it is bundled.

### A5 — Seven services, nine boxes, eight directories. RESOLVED

Architecture's map draws nine boxes and says "seven small services"; the Spec's repo layout has
eight service directories and no `analytics`. **Eight deployables**: gateway, connector,
classifier, extractor, resolver, pipeline, nudge, notifier. Analytics is not a service —
§11 already assigns matview refresh to `pipeline` (debounced 5 s after append).

### A6 — `Rejected` / `Dormant` are drawn as stages in the prototypes and defined as statuses. RESOLVED

`status` is authoritative. The server computes a `display_stage` field: when
`status ∈ {rejected, withdrawn, accepted, dormant}` the row shows that label, otherwise the
stage label. Computed server-side, because the client is not allowed to derive a stage
(README §State management).

### A7 — The `draft` stage is unreachable. RESOLVED

No event in the catalogue produces it, and quick-add emits `applied`. It is excluded from every
metric anyway. **Dropped from the seeded `stage_defs` in v1**; depth 0 stays free so a `saved`
event can reintroduce it later without renumbering.

---

## B. DDL in §04 that does not run, or runs unsafely

| # | Problem | Fix |
|---|---|---|
| B1 | `create type confidence as numeric(3,2)` is not valid SQL — `CREATE TYPE` has no such form | `create domain confidence as numeric(3,2) check (value between 0 and 1)` |
| B2 | `application_events.source_id references sources` but `sources` is created *after* it | reorder: `sources` before `application_events` |
| B3 | `uuid_generate_v7()` does not exist in stock Postgres 16 (core `uuidv7()` lands in 18) | ship a plpgsql `uuid_generate_v7()` (RFC 9562 layout) in migration 001; also `create extension citext, vector` |
| B4 | Idempotency key `unique (application_id, type, occurred_at, evidence_ref)` is defeated by a NULL `evidence_ref` — NULLs are distinct by default, so human-authored events duplicate freely | `unique nulls not distinct (...)` (PG 15+) |
| B5 | `current_setting('loop.user_id')::uuid` throws whenever the GUC is unset (migrations, maintenance, cron) and the policy does not bind the table owner | `current_setting('loop.user_id', true)` + `force row level security` on every user-scoped table; per-service roles with exactly the §04 grants (only `pipeline` holds INSERT on the log) |
| B6 | `seen_messages` has no `user_id`, so no RLS policy can be written on it | denormalise `user_id` from `mailbox_accounts` |
| B7 | `companies` is global with `unique (canonical_name)` **and** `unique domain`: two different companies sharing a name cannot coexist, and in multi-tenant, aliases learned from user A leak to user B | `unique (canonical_name, coalesce(domain,''))`; learned aliases move to a user-scoped `company_aliases (user_id, company_id, alias)`; the global row keeps only the public domain→name fact |
| B8 | `stage_defs.stale_after_days` is `not null` but `draft` has no value | moot — see A7 |
| B9 | `interviews.calendar_event_id` is globally unique, so two tenants with the same event id collide | add `user_id`; `unique (user_id, calendar_event_id)` |
| B10 | `app_phase_reach` is refreshed every ~5 s; without a unique index `REFRESH … CONCURRENTLY` is impossible and every refresh takes an `AccessExclusiveLock` that blocks reads | unique index on `(id)`, refresh concurrently |
| B11 | `app_phase_reach` inner-joins `application_events`, so an application with zero events vanishes from the funnel *denominator* | `left join` |
| B12 | `sources`, `interviews`, `comp_offers` have no `user_id`, so RLS needs a join per row | denormalise `user_id` onto every user-scoped satellite (index-backed policies) |
| B13 | §11's `first_human_at` filters on `e.type in ('recruiter_reachout', …)`, but `recruiter_reachout` is a **stage key**, not an event type — the predicate can never be true, so "median days to first human reply" would always be null | use the Architecture sheet's definition instead: the first inbound event that is neither the application itself nor an automated acknowledgement |
| B14 | pgmq is not in the PGDG apt repository; building it pulls `postgresql-server-dev-16` → `llvm-19-dev` into an image meant for a free-tier ARM box, and forces a rebuild on every minor Postgres bump | the queue is ~90 lines of SQL in an `mq` schema exposing pgmq's surface (`send`, `read` with visibility timeout, `delete`, `metrics`) over one table with `SKIP LOCKED`. The spec's own reason for pgmq — "transactional with the data it describes; visibility timeouts are enough at this volume" — holds identically. Swapping back is a search-and-replace of `mq.` for `pgmq.` |
| B19 | Registering `@fastify/static` **after** the routes silently discards a later `setErrorHandler`, so every 500 fell through to Fastify's default — which serialises the exception message. `/api/stats` was returning SQL text and Postgres error codes to the client | register the error handler first, before any plugin or route. the ordering is pinned by `backend/tests/test_api.py::TestEveryFailureWearsTheSameEnvelope`, because it is exactly the sort of thing a refactor undoes — the Fastify plugin hazard itself is gone with Fastify |
| B20 | The SPA fallback returned `index.html` for *every* miss, so a request for a stale `/assets/index-abc.js` got HTML with a 200. The browser refuses it on MIME grounds and the user sees a blank page with a console error naming neither the file nor the cause | only navigations get the shell; anything with a file extension, or that did not ask for `text/html`, gets an honest 404. Also `wildcard: true`, since `false` snapshots the directory at boot |
| B21 | The service worker cached the app shell cache-first. A deployed update therefore never reached an installed PWA: the cached `index.html` kept requesting a bundle hash that no longer existed | navigations are network-first with a cache fallback — which is what "offline read-only mode" actually needs. Hashed assets stay cache-first, because those really are immutable |
| B15 | The circulating plpgsql UUIDv7 snippet (`b'0111' \|\| x::bit(8) >> 4`) is wrong on Postgres: `\|\|` and `>>` share a precedence level and associate left, so the shift applies to the *concatenated* string and every id comes out version 0 | integer masks: `(byte & 15) \| 112` for the version, `(byte & 63) \| 128` for the variant. Verified against a live database, not assumed |

---

## C. Specified but underspecified — defined here

| # | Gap | Definition adopted |
|---|---|---|
| C1 | The Today headline "must never be a static string" but no generation rule is given | `n` = distinct applications with a forward event (stage_advanced to greater depth, `interview_scheduled`, `offer_received`) in the trailing 7 days. `n ≥ 1` → *"{Word(n)} moved / forward / this week"*. `n = 0, live > 0` → *"{Word(live)} applications / waiting"*. `live = 0` → *"Nothing / to track yet"*. Server returns `headline: {lines: string[]}` (max 3). No cheer path when `n = 0`. |
| C2 | Row `flag` is "precomputed server-side" but its precedence is never stated | deadline within 72 h → `Due {when}`; offer with `decide_by` → `decide by {date}`; dormant or quiet past p90 → `quiet · past your p90`; else empty. One value, first match wins. |
| C3 | `follow_up_due` is "batched into the daily slot"; the slot is never named | **18:00 user-local**, matching the `prepare` push. `deadline` fires on its own 72 h / 12 h schedule outside the cap. |
| C4 | Suggestions ranked "by urgency then depth" | rule order `deadline > prepare > follow_up_due > let_it_go`; within a rule, earliest `due_at`, then greater stage depth. Max 3. |
| C5 | Comp is "currency-normalised, gross annual" with zero third parties and €0 budget | store native currency always; render in the user's display currency (default EUR) using a checked-in dated rate table `rules/fx.yaml`; every converted figure is labelled with the rate date; currencies absent from the table are listed separately rather than folded in. No network FX call, ever. |
| C6 | Single-tenant authentication is never specified — §13 assumes a cookie session, §18 puts signup in P4 | see **OPEN-4** |
| C7 | `/api/today` returns `recent_events[≤8]`, prototype draws 4 | return 8, client renders what fits |
| C8 | Backfill option "everything" has no bound | cap at 60 months, state the cap in the UI |
| C9 | Model timeout "parks" a message; nothing drains the park | `pg_cron` re-publishes parked messages every 15 min, max 6 attempts, then it becomes a `low_confidence` review item — which is what failure state F4 already promises the user |
| C10 | `went_silent` uses `2 × p90 dwell`, but p90 needs ≥5 transitions to exist (§11 gate) | fall back to `stale_after_days` when the gate is unmet; `threshold_used` in the payload records which one applied (the field already exists for this) |
| C11 | "No table ever stores message bodies" (§04) vs. "one queue per stage of the pipeline" (§02) — a queue is a table, so message text necessarily lands in one | text travels in the queue only while a message is in flight and the ack deletes the row; the **dead-letter path strips it**, because that is the only path where a payload persists indefinitely. Nothing is lost: `seen_messages` makes every message replayable from the provider by id, which is what a replay log is for. A `stripBodies()` helper enforces it and `deadLetter()` is the only caller |
| C12 | §12 schedules deadline pushes at 72 h and 12 h, but the prototypes show a deadline *card* at "in 3 days" and a row flag reading "Due Sunday 23:59" long before any push | separate the two: the card and the flag appear 7 days out, the interruption stays at 72 h / 12 h. A deadline you can see coming is calm; a deadline that appears with the push is a scare |
| C13 | "Awaiting them" gates the follow-up rule but is never derived | the ball is in the user's court in `take_home`, `offer` and `negotiating`, or whenever an unmet deadline is open; otherwise it is with them |
| C14 | The signal carried one `role`, and it was the *normalised* one — so the pipeline stored `backend engineer` and the interface showed it | the signal carries both: `role` as written in the message, for display, and `role_normalised` for the resolver to embed. Conflating a comparison key with a display string is how lower-cased text ends up on screen |
| C15 | An ATS acknowledgement names the role in its body, but rung 1 only extracted a role for screening invitations — so most rows read "Unknown role" | added a body `role` capture to the acknowledgement patterns for the vendors whose wording is stable. Where the vendor genuinely does not state a role, "Unknown role" is the honest answer and stays |

---

## D. The six open decisions of §19 — recommendations

| # | Question | Recommendation |
|---|---|---|
| D1 | Which mail providers are actually in use | **OPEN-2** |
| D2 | May a take-home deadline break quiet hours? | **Yes** (spec default). It is the only alert whose silence has an unrecoverable cost. |
| D3 | Do dormant applications stay in the funnel denominator? | **Yes** (spec default). Removing them makes every ratio flattering and false; ghost rate exists precisely to name that outcome. |
| D4 | Timezone: device or user setting | **User setting.** `users.tz` already exists (default `Europe/Rome`); the device timezone only prefills it at onboarding. A device-derived tz would move the 03:00 dormancy job every time you travel. |
| D5 | Auto-merge at ≥0.93 cosine, or always ask | **Auto-merge, made reversible.** Merging stays automatic outside the listed exclusions, but each automatic merge writes a reversible merge record and posts an FYI card to the review queue with a one-tap Undo for 14 days. Always-asking floods the queue with cases the resolver is right about; an irreversible silent merge is the failure the spec fears. This gets both. |
| D6 | Retention of resolved review excerpts | **Delete with the item** (spec default), but persist the *redacted structural pattern* of the match (template shape, no names, no addresses, no free text) so rule-writing still improves. |

---

## E. Delivery dependencies — things only you can supply

- **E1** Google Cloud OAuth client (desktop type, unverified, your address as a test user), plus
  optionally a Pub/Sub topic. Until it exists the connector runs against the fixture-replay stub
  server, which exercises the identical code path.
- **E2** The project's go/no-go — ≥0.85 application-level recall over a 12-month backfill with
  zero wrong merges — is only measurable on your real inbox. I build the harness, the
  anonymiser and a synthetic corpus; the real measurement is a command you run.
- **E3** Production host (Oracle ARM free tier / a home box behind a tunnel / dev-only for now).
- **E4** Local Postgres is 14; everything runs on the Postgres 16 container. No local install.
- **E5** UI copy is English, as designed. Conversation stays Italian.

---

## F. Found by using it

Defects that only a person with a year of their own mail in the database can find, and what
each one turned out to be underneath.

### F1 — "Live applications" counted rows nobody had closed, not applications in progress. RESOLVED

`status` is `live` from the moment an application is created until something moves it: a
rejection, the nightly sweep, or a human. Nothing else does. So a mailbox read for twelve
months accumulates dozens of `live` rows nobody has thought about since spring, and the
counter that leads the product read fourteen where the truth was four.

The sweep is not the fix. It marks an application `dormant` past its stage's staleness and
`presumed_closed` past ninety days of silence — but it is a cron job on a box that may have
been off, it has been broken before for two days without anybody noticing (migration 015), and
its verdict lands in the database seconds-to-hours after the truth changed. A figure on a
screen should not depend on whether a job ran last night.

**Adopted.** A third derived value beside `status` and `phase`, computed from the row every
time it is read:

```
closed — status ≠ live, or presumed_closed, or silent past the closure threshold
stale  — quiet past this stage's threshold (stale_after_days, or 2 × your own p90)
active — moving, waiting on you, or with an interview in the calendar
```

in that order, with two rules that outrank silence: an uncancelled interview in the future
always reads `active` — a loop booked six weeks out is a long quiet gap that means the
opposite of what quiet usually means — and `take_home`, `offer` and `negotiating` are never
written off, which is the same exemption the sweep already makes because the ball is in the
user's court.

The closure threshold is **90 days**, and **60 days for the `sent` phase** — `applied` or
`acknowledged` and nothing since. The shorter number is not a guess about people; it is a
statement about what is being waited for. Past a screening call there is a panel to convene
and a calendar to fit, and the evidence for 90 days is in `PRESUMED_CLOSED_DAYS` (the longest
observed revival across a real twelve-month mailbox was 20 days). Before any human has replied
at all there is nothing to convene. Two months of that is a no that nobody typed.

`backend/loop/domain/activity.py` holds the ladder and `backend/tests/test_activity.py` its tests;
the SQL that asks the same question of a whole table is built from the same constants, next to
each, because a threshold that changes in one place and not the other is worse than either
value alone.

### F2 — Every ratio on the statistics page showed an em dash beside a funnel full of numbers. RESOLVED

Same cause, and it is the reason F1 is worth the surgery rather than a relabelled counter. The
display gates in §11 are counted in *closed* applications — eight of them before a percentage
is honest — and "closed" meant `status <> 'live'`. On a real mailbox most applications are not
rejected, they stop replying, so the denominator crawled and the gate never opened. The page
withheld figures it had ample data for while showing a funnel with twenty applications in it,
which reads as broken rather than as careful.

The metric cohort is now judged by `activity`: a process silent for three months counts as the
closed application it is. Ghost rate follows the same correction — it counts everything that
closed without anybody ever saying no, which is precisely what the number claims to measure,
and it was previously blind to every application the sweep had not reached. `channel_effectiveness`
is read inline for the same reason; the view's `status = 'dormant'` under-reports by exactly
that set.

Nothing about the gates themselves moved. Eight closed applications, five transitions, three
first-touch applications, two quarters — unchanged.

### F3 — Four controls on the application record did nothing at all. RESOLVED

`Draft follow-up` and `Correct stage` had no handler in the desktop drawer. `Open thread` was
wired to `posting_url`, which is null on most rows, so it sat disabled and unexplained.
`Archive` fired a mutation with no success and no failure path — the drawer closed whatever
happened, including on a 403. In the bulk bar, `Set stage…` was permanently disabled and
`Export CSV` linked at the flat export route and handed back the whole account while sitting
under a line reading "3 selected".

The interesting one is `Draft follow-up`, because it had been *written* — the mobile detail
view called `/api/suggestions/follow_up_due:{id}/draft`, a key that exists only if a nudge rule
happened to fire for that application. For every other row it 404s, and the sheet showed a
loading skeleton for ever. The composition never needed a suggestion, so there is now
`GET /api/applications/{id}/draft` beside it, and the sheet renders the failure rather than
spinning on it.

Corollary, applied everywhere: a mutation with no `onError` is a control that lies. Each one
now reports what happened, reading `error.code` and never `error.message`, which is the §13
contract.

### F4 — The board opened on twelve months of history. RESOLVED

`/api/applications` defaults to `activity=open` — active and stale, everything not written off
— and the filter is applied in SQL rather than to the fetched page, because a filter applied
after `limit` returns a short page and calls it the pipeline. History is a tab, with its own
count, one click away. A quiet application stays in the default view: it is the one that most
needs a follow-up, and hiding it would be the same mistake in the other direction.

### F5 — Figures rendered with fourteen decimal places. RESOLVED

`stage_dwell_in.p50_days` is a percentile over epoch seconds, so median time in stage arrived
as `12.416666666666666` and the desktop printed all of it. The server now rounds it and ships
the string to print beside the number, the same way every other figure in this API already
travels with its own formatting. The client formats nothing.

---

## Owner decisions — settled

- **OPEN-1 → P0 through P3.** The full system, RLS on from migration 001 so the multi-tenant
  step stays a deployment flag. P4 (public signup, OAuth verification, CASA, quotas, billing)
  is explicitly out.
- **OPEN-2 → Gmail only.** Consequences, applied throughout: the connector implements Gmail
  `watch` + `history.list` + backfill and Google Calendar incremental sync, and nothing else.
  No `imapflow` dependency, no IDLE loop, no `uidValidity` handling, no
  `POST /api/mailboxes/imap`. Failure state **F3 (IMAP login refused) is not built** — it has
  no trigger without IMAP; it ships with IMAP. F1, F2 and F4 are built. `mailbox_accounts`
  keeps `provider` and the cursor as an opaque `jsonb` so adding IMAP later is a new connector
  module, not a schema migration.
- **OPEN-3 → Full rung-3 adapter, engine off by default.** The extractor implements the whole
  model contract (strict JSON schema, temperature 0, one attempt then abstain, Art. 9 deny-list
  enforced after the call, confidence × 0.9, message text fenced as data). With
  `MODEL_BASE_URL` unset the rung abstains and the message becomes a `low_confidence` review
  item — which is failure state F4, so F4 is the default development posture and is exercised
  by every run. `llama.cpp` and `vLLM` ship as opt-in compose profiles; hosted requires
  `ALLOW_HOSTED_MODEL=true` plus a per-user consent event naming the processor.
- **OPEN-4 → Passkey (WebAuthn) with a recovery password.** Registration seeds the single user
  from the CLI; a passkey is enrolled at first login. `@simplewebauthn/server` +
  `@simplewebauthn/browser`, resident key, user verification required. Recovery is an
  argon2id password shown once at seed time — no email, so §12's no-send-path rule stays
  absolute. Session is the cookie described in §13 (HttpOnly, SameSite=Lax, Secure) plus a
  CSRF token on every mutation. Rate limit: per-IP on auth routes, per-user on mutations.

- **LIB-1 → Prefer maintained libraries over rewriting.** Anything that an existing,
  well-tested, maintained library already does is used from that library rather than written
  here. Hand-writing is the exception and carries the burden of proof, not the default — a
  bespoke implementation is a liability the moment it works: it has one maintainer, one test
  suite, and no other users finding its bugs.

  The exceptions that survive this rule are the ones already argued in place, and each names
  the property a library cannot supply:
  - `loop.domain` stays dependency-free — the differential harness and the sub-second test
    suite are load-bearing, and both depend on a core that imports nothing.
  - A byte-identical wire contract (the P3 response rules in `loop/api/serialise.py`) —
    response models and serialisation frameworks change exactly the bytes the contract fixes.
  - SQL a reviewer must read literally (the migration runner) — a migration DSL is a
    translation layer over the statements that *are* the review object.

  Everything else follows the rule. First application, the chat assistant: the `openai` SDK
  speaks the model wire format, LangGraph runs the agent loop, `sse-starlette` emits the
  event stream, `eventsource-parser` reads it and `react-markdown` renders the answers.
  Existing hand-rolled code (the Google client) is history, not precedent: it stays until
  touched, and the next substantial change to it is the moment to replace it with a
  maintained equivalent that can still be tested against a stub over the real protocol.
