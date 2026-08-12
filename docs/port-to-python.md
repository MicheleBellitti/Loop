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

**Status: built, and green on the committed corpus.** `loop/ladder/` is the
classifier, the registry over `rules/ats/*.yaml` and rungs 1 and 2 behind an
`ExtractionRung` protocol; `loop/harness/` is the corpus reader, the runner and
the divergence table. On `fixtures/` the Python reads the same 19 of 29 messages
the TypeScript reads, and fails on exactly the same two — both stale fixtures
with no From display name, which is where the rules were rewritten to read the
employer.

The real corpus needs the mailbox once:

```
npm run export:baseline                                    # writes fixtures/private/
uv run --extra ladder python scripts/diff_against_ts.py    # prints only disagreements
```

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

### P2 · Connector, resolver, pipeline

Gmail sync, entity resolution, the single writer. Still headless.

*Done when*: a full 12-month backfill in Python produces the same applications
as the TypeScript database — same companies, same stages, same event counts.
Differences here are the company-dedup improvements from §3.3, and they should
be *fewer* rows, not different ones.

### P3 · FastAPI

Port the read API. **Keep the response contract byte-identical** — same field
names, same precomputed `display_stage`, `days_quiet`, `flag`.

*Done when*: the existing React PWA, unmodified, points at the Python API and
works. That is a free end-to-end test of the whole surface, and it is only
available if the contract is preserved.

### P4 · The ML that justified the rewrite

Real embeddings, re-tuned thresholds, spaCy NER for company and role, the model
call moved out of the transaction, the stage detector rebuilt.

*Done when*: extraction on the corpus beats the TypeScript baseline, measured
with the harness from P1.

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

Do not delete the TypeScript version when P0 starts. It is the reference for the
differential harness and it is the thing that currently reads the mailbox. Retire
it when P3 passes — when the PWA runs against the Python API — and keep the
final TypeScript database dump as the fixture the port is diffed against.
