# Loop, the backend

Python. `../docs/port-to-python.md` is the plan it was built to; this is where
it landed, and as of the commit that deleted `packages/` and `services/` it is
the only implementation there is.

## Where things come from

**The migrations are here now**, in `migrations/`. They spent the port under
`packages/db/migrations/` for one reason: two implementations talked to the same
database, and two copies of a schema drift while a differential harness depends
on them being the same. That reason is gone with the TypeScript.

**`rules/ats/*.yaml` is data, not code.** Twelve vendor templates plus Allibo,
rewritten against real mail. `LOOP_RULES_DIR` overrides where they are read
from; nothing else does.

**The domain was re-derived, not translated.** From `../docs/decisions.md`,
which records the roughly twenty places the original Engineering Spec was wrong
and why. The assertions came first; the implementation was written to satisfy
them.

## Layout

    loop/domain/     the fold, stages, thresholds, nudges, wire codecs — pure
    loop/ladder/     classifier, rule registry, rungs 1–3, the signal
    loop/resolver/   embedder, matching, company identity, intent → event — pure
    loop/connector/  a Gmail message and an .ics, read — pure
    loop/google/     the API client, the sealed secret, the mailbox row
    loop/db/         asyncpg, the tenant session, the queue, migrations, rebuild
    loop/services/   the eight long-running processes
    loop/api/        FastAPI, built against the client as the specification
    loop/runtime/    the log: allow-listed fields, redacted secrets
    loop/harness/    the corpus, the runner, the divergence table
    migrations/      numbered .sql — schema, RLS, grants, the queue, the sweeps
    rules/ats/       the vendor templates
    fixtures/        the golden corpus, plus negatives that must be dropped
    scripts/         seed, migrate, replay, e2e, the corpus gate, the stub Google

`loop.domain` has no runtime dependencies and imports nothing outside the
standard library, which is why its tests run in a tenth of a second.
`loop.ladder` adds PyYAML, httpx and Pydantic and depends on the domain; nothing
depends on the harness.

The rungs sit behind an `ExtractionRung` protocol and the ladder is a pure
function of a message and a context. That is what fixes §3.1: the caller reads
what it needs, closes its transaction, runs the ladder — model call and all —
and opens a second short transaction to append the event. `ExtractorService`
hands the ladder to a worker thread, so an inference blocks neither the event
loop nor a pooled connection.

## Running it

    uv sync --group dev
    uv run --extra ladder pytest
    uv run ruff check .
    uv run --extra ladder --extra api --extra db --extra connector --extra push mypy loop

    uv run python scripts/test_db.py up        # Postgres 16 on :55432
    DATABASE_URL=postgres://loop:loop@localhost:55432/loop uv run --extra api \
      --extra db --extra connector --extra push --extra ladder pytest

    uv run --extra ladder python scripts/corpus_gate.py     # the §17 gate
    uv run --extra ladder python scripts/replay.py          # the fixtures
    uv run --extra api --extra db --extra connector --extra ladder \
      python scripts/e2e.py                                 # the whole pipeline

## The differential harness, now that the reference is gone

`scripts/diff_against_ts.py` reads `fixtures/private/ladder-baseline.jsonl`,
which pairs every real message with the verdict the TypeScript gave it. Only
`scripts/export-baseline.ts` could write that file, and it needed the TypeScript
classifier, rules and extractor — all of which have been deleted.

That is recoverable, not lost. Commit **`0ceb07c`** is the last one holding
them — the parent of the first commit on the branch that removed them, and an
ancestor of `main`, so it is never collected:

    git checkout 0ceb07c
    npm install && npm run export:baseline    # writes fixtures/private/
    git checkout -

Worth tagging so it has a name rather than a hash. The credentials this branch
was pushed with could not create one:

    git tag -a typescript-final 0ceb07c -m "the TypeScript reference"
    git push origin typescript-final

The baseline it writes is git-ignored and needs no database, no network and no
mailbox afterwards, so the comparison stays reproducible after the mail has
moved on. `loop/harness/divergences.py` is the table of deliberate differences,
one entry per change with the section of `../docs/port-to-python.md` that
justifies it; a difference the table cannot explain fails the diff.

## State

| Phase | | |
|---|---|---|
| P0 | schema and domain, headless | **done** |
| P1 | the ladder and the differential harness | **done** — 974/1000 identical, 26 deliberate, nothing unexplained |
| P2 | connector, classifier, extractor, resolver, pipeline, nudge, notifier | **done** — `python -m loop <service>` |
| P3 | FastAPI, response contract byte-identical | **done** — every route the client calls |
| — | the TypeScript retires | **done** — commit `0ceb07c` is what it was |
| P4 | real embeddings, spaCy, the stage detector, the corrections loop | |
| P5 | the interface | |

## What the port fixed on the way through

Defects found by writing the same system twice, each one recorded where it was
fixed and in the commit that fixed it.

**Live, in production.** The nightly dormancy sweep had been failing for two
days — migration 010 added `sweep_dormancy(integer default 90)` without dropping
the zero-argument version, so cron matched two candidates and refused to choose.
Nothing had gone dormant since. Migration 015.

**Never worked at all.** Passkey registration and passkey login: the challenge
was consumed with `update … returning` of the column it had just nulled, which
on Postgres 16 returns the new value. Both verify routes always answered
`no_challenge`, and the recovery code was the only way in.

**Would have failed on the first request.** Four gateway routes and the
extractor's fourth rung need grants nobody has, and only worked because the
services connected as a superuser and bypassed row-level security entirely.
Migration 014, and the write path moved so the API may create an application and
never move one.

**Silently wrong.** Every 404 from Google meant "history expired", so a deleted
message relisted the whole mailbox — and the relist never re-established the
cursor, so once stale it stayed stale for ever. Every 403 meant "sign in again",
including quota errors that signing in cannot fix. The access token was
refreshed once per message ingested. An `.ics` was never unescaped, so every
interview location in the database read `Dinova\, Via Francesco Zanardi\, 51`. A
correction recorded the wrong before-value for every field but one.

**Found in the last pass, closing the port.** `SESSION_SECRET` defaulted to a
string printed in the source, from which any reader could mint a CSRF token for
any session. Every rate limit had been dropped, including five per fifteen
minutes on the public recovery password. The projection rebuild blanked two
columns nothing in the log could put back. And the no-send-path lint the §12
invariant rests on had never once read a `.py` file.
