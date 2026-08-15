# Porting Loop to Python

The TypeScript implementation is not the deliverable. It was the instrument that
turned a design document into a validated specification: it found roughly twenty
places where the spec was wrong, and it proved on a real twelve-month mailbox
that mailbox-driven extraction works. That knowledge is language-neutral. The
code is not.

The rewrite is justified by one thing above all others — the owner reads Python
and not TypeScript, and a codebase you cannot read at 2 a.m. when the connector
stops is a codebase that will not be fixed. The second reason is real but
secondary: every genuinely hard part of this system is ML-shaped, and Python is
where that work lives.

This document says what to carry, what to re-derive, what *not* to reproduce,
and in what order — so that at every step something runs and can be measured.

---

## 0 · The organising principle: port differentially

This is not a rewrite on faith. There is a working reference implementation and
a real corpus of 529 classified messages from the target mailbox.

**Every stage of the port is validated by running both implementations over the
same messages and diffing the output.** Not "does it look right" — `assert
python_result == typescript_result`, message by message, until a difference is
either a fixed bug or an accepted improvement.

That is the single most valuable asset the TypeScript version leaves behind, and
the port should be organised around exploiting it. Build the differential
harness in phase 1, before anything else that could drift.

```
scripts/diff_against_ts.py --ids-from fixtures/private/survey.json
  → per message: classifier score, rung, intent, company, role, confidence
  → prints only the disagreements
```

---

## 1 · Carries over unchanged

**The eleven SQL migrations.** `packages/db/migrations/*.sql` — schema, RLS with
per-service grants, the append-only trigger with its one erasure hatch, the
`SKIP LOCKED` queue, the metric views, both dormancy sweeps. Postgres does not
care what language talks to it. This is the artefact with the twelve DDL
corrections in it (§B of `decisions.md`) and it should be copied, not retyped.

Keep the sixty-line runner over numbered files rather than adopting Alembic. The
reasoning in `packages/db/src/migrate.ts` still holds: this schema's hardest
parts are policies, grants and a trigger, all of which are only expressible as
SQL, and a migration DSL puts a translation layer over the exact statements a
reviewer needs to read literally.

**`rules/ats/*.yaml`.** Twelve files, rewritten against real mail. Data, not
code. `pyyaml` reads them; the schema validation moves from zod to Pydantic.

**`docs/decisions.md`.** The most valuable file in the repository and it contains
no code: every ambiguity in the original spec, why the fold rule as written
froze every application, why the serial-id tie-break broke replay determinism,
why the CSRF token was unrecoverable, the six product decisions and their
consequences. **Re-derive the domain from this document, not from the original
Engineering Spec.** That difference is an afternoon versus a fortnight.

**Every threshold and its justification.** `packages/domain/src/thresholds.ts` is
mostly prose explaining numbers. The numbers move to a Python module; the prose
moves with them, because a threshold without its reason gets tuned by whoever
touches it next.

---

## 2 · Re-derived, from the tests

The domain is 2,253 lines of pure logic and it is where a silent porting error
would hurt most. Do not translate it line by line. Port the **assertions**, then
make them pass.

Roughly thirty cases, all in `packages/domain/src/*.test.ts`:

| Invariant | Why it is not obvious |
|---|---|
| Replaying events in any arrival order yields identical state | Tested over 200 permutations. The spec's tie-break on the serial `id` breaks it, because `id` is arrival order. |
| A 0.99 acknowledgement does not outrank a later 0.94 stage change | The spec's literal rule freezes every application at `acknowledged` forever. |
| Only `human_corrected` pins, and only the field it names | Quick-add also writes at confidence 1.0; if that pinned, a hand-added application would be deaf to its own mailbox. |
| A terminal status freezes automated stage movement; a correction unfreezes it | |
| `went_silent` changes status and never the stage | The funnel keeps the application in its denominator precisely because the stage stands. |
| `applied_at` falls back to the earliest acknowledgement | Real applications are submitted through web forms that email nobody; without this every ratio loses its denominator. |
| A new inbound signal revives a dormant application but never a rejected one | |
| Events below the confidence floor never vote | |
| Article 9 keys are dropped from model output and counted, never silently passed | |
| Quiet hours wrap past midnight and survive both DST transitions | |

Write these as pytest first. They are the specification.

---

## 3 · Do not reproduce these

Known defects in the TypeScript version. Porting them faithfully would be the
worst possible outcome of a rewrite.

### 3.1 The model call happens inside a database transaction

Found while getting a local model running. The extractor opens a transaction,
calls rung 3, and waits — so the connection sits idle-in-transaction for the
whole inference. At `idle_in_transaction_session_timeout: 30s` Postgres killed
the connection mid-flight and the unhandled error took the process down. The
current fix raises the timeout to 180 s and couples two numbers that should not
be coupled.

**In Python with in-process inference this gets worse, not better**: a GPU call
holding a pooled connection for seconds will exhaust the pool under any
concurrency at all.

The structure to build instead:

```
read message + claim queue item   → transaction 1 (short)
run the ladder, including the model → no transaction, no connection held
append event + advance the queue   → transaction 2 (short, idempotent)
```

The idempotency key on `(application_id, type, occurred_at, evidence_ref)`
already makes the second transaction safe to retry, so nothing is lost if the
process dies between the two.

### 3.2 The stage detector is a placeholder

`stageFromTitle()` reads a calendar summary for keywords and **defaults every
unrecognised invite to `technical`**. That is why the pipeline shows a column of
identical "Technical" stages: it is not a reading, it is a fallback wearing a
reading's clothes.

What to build instead, in order of cost:

1. Use the model's `stage_hint`, which is already in the rung-3 schema and is
   currently discarded when rung 2 matched first.
2. Condition on the thread: a second invite from a company already at `hr_call`
   is more likely `technical` than another `hr_call`. The previous stage and the
   gap since it are strong features and are already in the event log.
3. Train on the user's own corrections. Every `human_corrected` event with
   `field: stage` is a labelled example, and the review queue generates more.
   This is the point where owning the ML end to end pays for the rewrite.

Until it is good, **abstain rather than default**. An unrecognised invite should
produce `interview_scheduled` with a null stage and let the phase be
`interviewing` — which is the claim actually supported by the evidence.

Done in P1, in both places the default was written: rung 2 no longer answers
`technical` to a titleless invite, and `stage_for_intent` no longer answers it
for `interview_invite`. **The same default appears a third time**, in
`services/resolver/src/events.ts`, as `signal.stage_hint ?? 'technical'` on four
lines. Porting that faithfully would put the guess straight back. When P2
reaches the resolver, a null `stage_hint` on an interview intent means the phase
advances and the stage does not.

### 3.3 Company deduplication is two heuristics in a trenchcoat

Today: match on sender domain, else on an alias key that strips everything but
letters and digits. That collapses "ION Group" and "iongroup", which was the bug
it was written for, and nothing harder. It still produces `Cradle` twice, and it
produced `Careers @ Jet` and `noreplyHRrecruitingTeam` as company names.

What to build:

- A **canonical registry** seeded from the ATS mail already processed: sender
  domain → employer, learned once and reused. `company_aliases` exists for this
  and is under-used.
- **Fuzzy matching** (`rapidfuzz`) as a second pass, with a threshold tuned on
  the corpus rather than guessed.
- **Embeddings over company names** for the residual, using the same encoder as
  the role matching.
- A **rejection list** for display names that are not companies — the current
  test is a regex against `noreply|recruiting|hrteam`, which is a list of things
  already seen rather than a rule.

Note that the resolver's cosine thresholds (`ATTACH_SINGLE 0.72`,
`ATTACH_MULTI 0.82`, `DEDUP_MERGE 0.93`) were tuned against a **lexical hashing
embedder**, not a real one. With `sentence-transformers` they must be re-tuned
against the corpus. Carrying the numbers across unchanged would be a silent
regression.

### 3.4 Smaller, known

- The generic body vocabulary matches LeetCode's marketing mail on "coding
  challenge" and files it as a take-home. Noise domains need excluding from the
  deterministic pass.
- Role extraction covers 10 of 24 applications. spaCy Italian NER is the obvious
  upgrade and is the second concrete reason Python wins here.
- The corpus in `fixtures/` is synthetic — written from the same reading of the
  spec that produced the rules, so CI has been confirming an assumption rather
  than testing it. Rebuild it from real anonymised mail (`scripts/anonymise.ts`
  exists and works) **before** trusting any measurement in the new codebase.

### 3.5 A compensation figure is not a formal offer

Found reviewing false positives against real mail, not in the differential
harness — both implementations share this defect, so a diff against the
TypeScript baseline cannot catch it. `rung3.ts`'s schema carries `comp` and
`intent` as independent fields (§ above, `services/extractor/src/rung3.ts`),
but the prompt never says they are independent *facts*: it asks the model to
extract both without ever stating that a number is not itself a proposal. Any
RAL mentioned mid-process — a screening question, a range disclosure, a
negotiation counter — gets read as `intent: offer`, because nothing in the
prompt distinguishes "a figure is present" from "a position is being offered".

What to build instead:

- **Prompt.** State explicitly that a populated `comp` does not imply
  `intent: offer`. `offer` requires a formal proposal — "siamo lieti di
  offrirti la posizione", "we are pleased to offer you the position" — not a
  compensation figure appearing in any other context. Give the model one
  contrastive example of each in the prompt: a negotiation message with a
  number and no offer, and an offer message with a number.
- **Fold.** Require corroboration before a single rung-3 `offer` signal alone
  advances status to `offer`/`accepted`: a second signal (a congratulatory or
  next-steps phrase, or an existing `interview_scheduled`/`negotiation` event
  earlier in the same application's log) rather than one low-context sentence
  deciding the terminal state outright. The same asymmetry the fold already
  applies to a 0.99 acknowledgement not outranking a later stage change
  applies here in reverse — a single high-confidence `offer` should not
  outrank an application's whole prior history either.
- **Measurement.** Add offer/negotiation as a labelled contrast pair to the
  eval set this fix is checked against (§3.6), so a prompt change here is a
  measured precision number, not a one-off tweak that regresses silently next
  time the prompt is touched.

*Done when*: offer-intent precision on the corpus — offers corroborated by a
subsequent congratulatory or next-steps message, not reversed by a later
rejection or negotiation-only signal — is measured, and the fix does not
regress it as thresholds move elsewhere.

### 3.6 A correction is read once, then discarded

`human_corrected` events already exist to pin one field (§3.2's third option
covers the stage detector). But the mechanism stops at the row it corrects:
today a company merged wrong (§3.3) or an intent misread (§3.5) becomes
invisible again the moment a human fixes it, because the correction changes
that one record and nothing that will see the same pattern tomorrow. The
review queue is already generating exactly the labelled data both problems
need to get better, and it is thrown away after being applied once.

What to build:

- **Every `human_corrected` event becomes a labelled example on write, not
  only a pin.** `{field, before, after, evidence}` appended to a corrections
  table, distinct from the application it patches.
- **Company merges corrected by a human seed the canonical registry (§3.3)
  directly** — ahead of the next fuzzy or embedding pass touching the same
  sender domain, so the same wrong split does not recur on the next message
  from the same recruiter.
- **A periodic re-tune** (triggered on corpus size, not a calendar) of
  `ATTACH_SINGLE` / `ATTACH_MULTI` / `DEDUP_MERGE` and the offer-confidence
  discount against the accumulated corrections, run through the harness from
  P1 so a re-tune is a measured change, not a guess.

*Done when*: a correction made this week measurably reduces the same class of
error next week on the harness — not just in the one row it was applied to.

---

## 4 · Order of work

Each phase leaves something that runs and something that can be measured.

### P0 · Schema and domain, headless

Migrations copied. Domain re-derived from `decisions.md` with the thirty
assertions as pytest. No I/O, no web, no database in the tests.

*Done when*: `pytest` is green and the fold's determinism property passes over
200 permutations.

### P1 · The ladder and the differential harness

Rules engine over the YAML, classifier, rung 1, rung 2. Then immediately
`survey.py`, `replay.py`, `diff_against_ts.py`.

*Done when*: replaying the 529-message corpus produces the same classifier
scores and the same rung-1/rung-2 verdicts as the TypeScript, or every
difference is a deliberate improvement with a note.

This is the phase that de-risks everything after it. Do not skip the harness to
get to the API sooner.

**Status: done. The gate is met on real mail.** Over 1000 real messages the two
implementations agree on 974 and differ on 26, every one of them explained by
the divergence table. Nothing unexplained.

`loop/ladder/` is the classifier, the registry over `rules/ats/*.yaml` and rungs
1 and 2 behind an `ExtractionRung` protocol; `loop/harness/` is the corpus
reader, the runner and the divergence table.

What the deliberate 26 amount to, on the same corpus:

| | reference | port |
|---|---|---|
| extracted | 14 | **28** |
| with a company | 14 | **23** |
| with a role | 3 | **7** |
| review queue | 87 | **68** |
| ignored as self-sent | 0 | 5 |
| dropped | 899 | 899 |

Twice the extraction from the same input, a review queue a fifth shorter, and
the classifier unmoved — every message either implementation drops, the other
drops too. The reference has no `schedule_screening` at all; the port reads ten.
And the names changed as much as the counts: `ISelection - Iagica srl [via
ALLIBO]` became `ISelection - Iagica srl`, `LeetCode` stopped being an employer,
and `Clara Villamayor` became `Prima`.

The real corpus needs the mailbox once:

```
git checkout 0ceb07c && npm install && npm run export:baseline
git checkout -                                             # writes fixtures/private/
uv run --extra ladder python scripts/diff_against_ts.py    # prints only disagreements
```

`0ceb07c` is the last commit holding the TypeScript. Tag it `typescript-final`
so it has a name: `git tag -a typescript-final 0ceb07c -m "the TypeScript
reference" && git push origin typescript-final`.

The baseline pairs each real message with the verdict this implementation gave
it, so the diff afterwards needs no database, no network and no mailbox, and
stays reproducible after the mail has moved on. It carries the classifier's
context — thread map, company domains, learned newsletters — on its first line,
because a reply on an owned thread is worth two points and diffing without the
same context compares two different questions.

**The deliberate differences live in `loop/harness/divergences.py`**, one entry
per change with the section of this document that justifies it, each predicate
written to match that change and nothing adjacent. A difference the table cannot
explain fails the diff. Registered so far: the §3.2 stage abstention, the known
thread reaching the vocabulary, §3.4 practice sites, and the §3.3 domain
fallback. Anything else is a porting error.

Three defects were found while porting and are fixed rather than carried:

- `htmlToText` writes `label <href>` and the generic tag strip removes it on the
  next line, so the posting URL the function exists to preserve never survived
  an HTML message. Parentheses now.
- The role capture's terminators are optional, so "Platform Engineer was sent to
  Nexi" fits inside the six-word ceiling and arrives as a job title. It is now
  cut at the first word no job title contains, which recovers the real one.
- `buildSignal` only ever sets `excerpt` below the review threshold, and it is
  only ever called above it — so the resolver's own ambiguity review items were
  raised with a null excerpt. A review card with nothing on it to judge by is
  not a review card. Carried on every signal now.

**Then the harness was pointed at what a diff cannot see.** A differential shows
where the two implementations disagree; it says nothing about where both are
wrong, which is where recall lives. An audit over the 84 review items and the
709 borderline drops — nine readers, each finding adversarially refuted before
it counted — confirmed 24 missed signals against 13 extracted.

Almost all of them were in the review queue rather than dropped, which is the
good news: a review item is visible and a drop is not. Exactly one real signal
had been lost silently, an Italian ATS acknowledgement scored to −2 by a bulk
penalty its vendor was not exempt from.

What that produced, all measured on the same 1000 messages:

| | before | after |
|---|---|---|
| extracted | 13 | 29 |
| review queue | 84 | 70 |
| dropped | 898 | 896 |
| companies named | 12 | 24 |

- The vocabulary had **no `schedule_screening` phrase at all**, and ten of the
  misses were exactly that. An Italian recruiter arranges the first call in
  prose, over several short messages, none of which say "colloquio".
- The bulk waiver named LinkedIn and Indeed. It now covers every vendor in the
  registry: an ATS sends in bulk because that is what it is.
- LinkedIn's InMail relay is the one sender whose display name is a person
  rather than the employer. Those now yield no company at all, which is the
  honest answer and the reason P2 needs the thread token above.
- `rules/ats/allibo.yaml` is new, and one Greenhouse template — a rejection
  whose subject is "Update for the … opportunity" and which never says no.

**Do not add a catch-all LinkedIn InMail template.** It was tried. Rung 1 runs a
vendor's own patterns *before* the cross-vendor vocabulary, so a pattern
matching `^Risposta al messaggio:` shadows the phrases: six messages that read
correctly as `schedule_screening` collapsed to `other`. Subject capture is
unreliable there too — "Opportunità lavorativa - Torino" yields *lavorativa* as
the company.

### P2 · Connector, resolver, pipeline

Gmail sync, entity resolution, the single writer. Still headless.

*Done when*: a full 12-month backfill in Python produces the same applications
as the TypeScript database — same companies, same stages, same event counts.
Differences here are the company-dedup improvements from §3.3, and they should
be *fewer* rows, not different ones.

**Carry the LinkedIn thread token.** The recall audit found that Gmail splits a
single LinkedIn conversation across several `thread_id`s — one InMail root at
`inmail-hit-reply@`, its replies at `hit-reply@`, and a nudge from
`messages-noreply@`. One AYES conversation is eight messages over four Gmail
threads. Every transport carries a stable join key in the body:

```
linkedin\.com/(?:comm/)?messaging/thread/(?<li_thread>2-[A-Za-z0-9=_-]+)
```

Nineteen messages in the corpus carry it, over six tokens. Keying identity on
this alongside `thread_id` is what lets a reply inherit the company and role
from the InMail that opened the conversation — and that inheritance is the only
correct source, because the replies name no employer at all. The ladder now
returns those with `company: None` on purpose rather than filing them under the
recruiter's name; the resolver is what makes them whole.

The same applies to the four `stage_hint: null` invitations from §3.2: a null
stage on an interview intent means the phase advances and the stage does not.

**The event envelope is nested on the wire and flat in the dataclass.** The
queue payload is `{user_id, application_id, event: {type, occurred_at, …}}`, and
four SQL sites build it that way — `sweep_dormancy`, `sweep_dormancy_all`,
`mark_interviews_held` and the presumed-closed sweep all call
`jsonb_build_object('event', jsonb_build_object(…))`. A decoder that expects the
flat shape drops every cron-produced event and dead-letters it after five
deliveries, which would look exactly like dormancy quietly not working. Decide
the nesting at the codec, not in the dataclass. `drain_parked` is a second trap:
it enqueues a four-key stub, not a `CandidateMessage`, so whatever validates
that queue has to tolerate it.

**Two things about the runtime, both load-bearing.**

The visibility timeout is granted once per *batch* at claim time and consumed
*serially* per message. A batch of 20 at 3 s each against a 60 s lease means the
twentieth message's lease expired before its handler started: it is being worked
twice, its `read_ct` is climbing on attempts no handler saw, and it dead-letters
early. Claim smaller batches, or extend the lease per message.

And `read_ct` is post-increment, so a first delivery reports 1 and the fifth
failure is the last one worth having — `>= MAX_ATTEMPTS`, not `>`.

**This port is the first time row-level security actually applies.** None of the
queue-driven TypeScript services issue `set local role`, and they connect as a
superuser, so the policies, the FORCE flags and every grant in migration 003 are
decorative on those paths — `withUser` is the gateway's alone. Doing it properly
surfaces failures that have never fired: an `update … where id = $1` with no
`user_id` predicate becomes `UPDATE 0` rather than an error, and grants nobody
has exercised turn out to be wrong (the gateway's quick add inserts into
`companies`, `applications` and `comp_offers`, and `loop_gateway` has insert on
none of them). Each one is a decision about the write path, not a transcription.

Also: `services/connector/src/{google,mailbox}.{js,d.ts}` are stale compiled
copies predating the attachment-hydration fix and are imported by nothing. Port
from `packages/google/src/google.ts`. And there are no connector tests in the
reference at all, which cuts both ways — nothing pins the behaviour, and nothing
will tell you when you break it.

**Status: done, all six processes.** `python -m loop <service>` starts one each.
The stale compiled copies are deleted.

Seven defects in the connector were fixed rather than carried, and each is worth
knowing because none of them announces itself:

- Every 404 mapped to `HistoryTooOld`, so a message deleted between the listing
  and the fetch relisted the whole mailbox. Now only the history path.
- The relist never re-established the cursor, so a stale history id stayed stale
  and every five-minute poll re-listed the same thirty days for ever.
- Every 403 set `needs_reauth` — including `accessNotConfigured` and
  `userRateLimitExceeded`, neither of which signing in again can fix, both of
  which showed the user the product's only full-screen failure.
- The access token was refreshed inside the per-message ingest: a 250-message
  backfill page cost 250 token round-trips.
- The calendar's `410 Gone` was caught by nothing, so an expired sync token sat
  in the row and every run threw.
- `showDeleted` was never set, which made the cancellation handling downstream
  dead code.
- An authorisation with no refresh token — which is what Google returns when a
  grant already exists — sealed `{}` over the working secret.

And `parse_ics` unescapes. RFC 5545 escapes commas in every text value, the
reference never undid it, and the location of every interview already in the
database reads `Dinova\, Via Francesco Zanardi\, 51`. Reprocessing would clean
them.

The body is not chosen in the connector. Both MIME halves go to
`normalise_message`, which is the reader the harness measured a thousand real
messages against — deciding between them here would put a second, unmeasured
reading upstream of everything P1 validated. The harness was re-run after the
port: 974/1000, unchanged.

**The thread map lags the pipeline, and that is a race.** The resolver reads
thread identity out of `application_events`, which only the pipeline writes. Two
messages on one thread resolved before the pipeline drains therefore find
nothing to inherit from and create two applications. A live mailbox delivers a
thread's messages minutes apart and never notices; a backfill delivers them
together, which is exactly where "replay split one application in two" came
from. Inherited rather than introduced, and left alone for now — the fix is
either for the resolver to remember its own recent decisions or for identity to
move off the event log, and neither is a change to make while the differential
is the thing keeping this port honest.

### P3 · FastAPI

Port the read API. **Keep the response contract byte-identical** — same field
names, same precomputed `display_stage`, `days_quiet`, `flag`.

*Done when*: the existing React PWA, unmodified, points at the Python API and
works. That is a free end-to-end test of the whole surface, and it is only
available if the contract is preserved.

**Status: every route the client calls exists.** Sign-in and passkeys, the
board and one application's history, Today, the review queue, statistics, the
write surface, the event stream, push, the export, the mailbox connection and
the health line. 414 tests.

Three things settled here that the plan left open.

**Who may write `applications`.** The reference's gateway inserted companies and
applications, stamped `last_user_action_at` on two routes and cleared
`merged_into_id` on a third — four statements needing grants `loop_gateway` has
never had, which have only ever worked because the services connect as a
superuser. The line is now drawn at creation: the gateway may bring an
application into existence, because quick add is the one place the user rather
than the mailbox is the source of truth, and every subsequent move of it goes
through the log. `last_user_action_at` is dropped outright — the projection
recomputes it seconds later — and undoing a merge is a `human_corrected` event
the pipeline applies.

**Absent is not null.** No route uses a response model. The client distinguishes
a key that is missing from one that is `null`, `1.0` goes over the wire as `1`,
and three encodings of the same numeric column travel on a single response. That
is an accident of the reference's driver rather than a design, and it is still
the contract.

**The stream needs a real socket to test.** httpx's in-process transport runs the
application to completion before returning a response, and a stream never
completes, so an SSE test through it deadlocks rather than failing. The suite
runs uvicorn on a port for that one case.

**The port is finished, and the TypeScript is deleted.** Commit `0ceb07c` is
the last one that holds it — which is what `npm run export:baseline` needs, and
therefore the only way to re-baseline the differential harness from a mailbox. Everything below this line is what the port did not do, and it is
therefore the plan for what comes next rather than a record of what happened.

The six capabilities that were TypeScript-only when the retirement was proposed
were ported first: rung 3, the projection rebuild, KEK rotation, the redacting
log, the embedder escape hatch and the user-seeding bootstrap. Five behavioural
divergences in the API were closed at the same time, two of them security
regressions rather than design choices. `backend/README.md` names them.

### P4 · The ML that justified the rewrite

Real embeddings, re-tuned thresholds, spaCy NER for company and role, the model
call moved out of the transaction, the stage detector rebuilt, the offer/comp
prompt fix (§3.5), and the corrections feedback loop (§3.6) that both the
canonical company registry and the offer-precision measurement depend on.

*Done when*: extraction on the corpus beats the TypeScript baseline, measured
with the harness from P1, and offer-intent precision (§3.5) is measured and
does not regress as the corrections loop (§3.6) re-tunes other thresholds.

### P5 · The interface

Now, and not before, rethink the UI. The server computes everything — no
statistic, no stage, no dormancy decision is made client-side — so any client is
thin, and that decision is what makes a native iOS app cheap rather than a
second implementation of the domain.

---

## 5 · Stack

| Concern | Choice | Note |
|---|---|---|
| API | FastAPI + Pydantic v2 | Pydantic replaces most hand-written validation |
| Database | asyncpg, SQL by hand | An ORM over this schema would hide RLS, which is the point of the schema |
| Migrations | the numbered-`.sql` runner | ~60 lines; see §1 |
| Queue | the existing `mq` schema | It is SQL. It does not change. Redis would add a second thing to operate for forty messages a week |
| Tests | pytest + testcontainers | Same split: pure tests everywhere, integration against a real Postgres |
| Embeddings | sentence-transformers, bge-small | Replaces `LexicalEmbedder`, which was a stand-in for exactly this |
| NLP | spaCy `it_core_news_lg` | Company and role extraction, §3.3 and §3.4 |
| Model | llama-cpp-python, or an OpenAI-compatible client | In-process only once §3.1 is fixed |
| Google | httpx, hand-rolled | Same reasoning as the TypeScript client: a base URL that is one env var makes the fixture-replay stub trivial and keeps the real code under test |

Estimate: **6,000–7,000 lines**, against 10,400 in TypeScript. Pydantic and the
absence of a shared type layer account for most of the difference.

---

## 6 · Repository layout

One repository. The pipeline and the API share the domain model and deploy
together; splitting them before the boundaries are known produces version skew
between two repositories that always change at the same time.

```
loop/
  migrations/           copied verbatim
  rules/ats/*.yaml      copied verbatim
  loop/
    domain/             fold, stages, thresholds, nudges, metrics — pure
    db/                 asyncpg, queries, RLS session helpers
    ladder/             classifier, rung1, rung2, rung3, resolver
    services/           connector, pipeline, nudge, notifier — long-running
    api/                FastAPI
  scripts/              survey, replay, diff_against_ts, reprocess, anonymise
  tests/
  fixtures/             rebuilt from real anonymised mail
```

The one justified exception is the **iOS app**: it has a release cadence imposed
by the App Store and shares no code with the backend. That belongs in its own
repository.

---

## 7 · What to keep running meanwhile

*Historic. This section set the retirement condition and the condition was met.*

Do not delete the TypeScript version when P0 starts. It is the reference for the
differential harness and it is the thing that currently reads the mailbox. Retire
it when P3 passes — when the PWA runs against the Python API — and keep the
final TypeScript database dump as the fixture the port is diffed against.

**What actually happened.** P3 passed at the route level and the audit that
followed found six capabilities and five API behaviours that had not been
ported at all, none of them visible from "the client works". They were closed
first; the deletion is the commit after them. The reference is preserved as a
commit rather than as a dump — `git checkout 0ceb07c` runs it, which is
strictly more than a dump would have given, and it is what re-baselining needs.

The `packages/` and `services/` paths throughout this document are left as
written. They are the provenance of every decision recorded here, and rewriting
them to point at files that no longer exist would make the argument harder to
check rather than easier.
