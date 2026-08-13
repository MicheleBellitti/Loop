# Loop, in Python

The port. `docs/port-to-python.md` is the plan; this is where it lands.

## Where things come from

**Migrations are not copied.** `packages/db/migrations/*.sql` stays the single
source of truth while both implementations run, because two copies of a schema
drift and the differential harness depends on them being the same database.
When the TypeScript side retires, that directory moves to `migrations/` here.

**Rules are not copied either**, for the same reason: `rules/ats/*.yaml` is data
and both readers parse the same files.

**The domain was re-derived, not translated.** From `docs/decisions.md`, which
records the roughly twenty places the original Engineering Spec was wrong and
why. The assertions came first; the implementation was written to satisfy them.

## Layout

    loop/domain/     the fold, stages, thresholds, nudges, wire codecs — pure
    loop/ladder/     classifier, rule registry, rungs 1 and 2, the signal
    loop/resolver/   embedder, matching, company identity, intent → event — pure
    loop/connector/  a Gmail message and an .ics, read — pure
    loop/google/     the API client, the sealed secret, the mailbox row
    loop/db/         asyncpg, the tenant session, the queue, migrations
    loop/services/   the six long-running processes
    loop/api/        FastAPI, built against the client as the specification
    loop/harness/    the corpus, the runner, the divergence table
    scripts/         replay.py, diff_against_ts.py

`loop.domain` has no runtime dependencies and imports nothing outside the
standard library, which is why its tests run in a tenth of a second. `loop.ladder`
adds PyYAML and Pydantic and depends on the domain; nothing depends on the
harness.

The rungs sit behind an `ExtractionRung` protocol and the ladder is a pure
function of a message and a context. That is what fixes §3.1: the caller reads
what it needs, closes its transaction, runs the ladder — model call and all —
and opens a second short transaction to append the event.

## Running it

    uv sync --group dev
    uv run --extra ladder pytest
    uv run ruff check .
    uv run --extra ladder mypy loop

    uv run --extra ladder python scripts/replay.py               # the fixtures
    uv run --extra ladder python scripts/diff_against_ts.py      # against the reference

The diff needs a baseline, which the TypeScript writes from the mailbox once:
`npm run export:baseline` in the repository root. It lands in
`fixtures/private/`, which is git-ignored, and pairs every real message with the
verdict the reference gave it — so the comparison afterwards needs no database,
no network and no mailbox.

## State

| Phase | | |
|---|---|---|
| P0 | schema and domain, headless | **done** |
| P1 | the ladder and the differential harness | **done** — 974/1000 identical, 26 deliberate, nothing unexplained |
| P2 | connector, classifier, extractor, resolver, pipeline, nudge, notifier | **done** — all six processes, `python -m loop <service>` |
| P3 | FastAPI, response contract byte-identical | **done** — every route the client calls |
| P4 | real embeddings, spaCy, the model out of the transaction | |
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
extractor's fourth rung need grants nobody has, and only work today because the
services connect as a superuser and bypass row-level security entirely.
Migration 014, and the write path moved so the gateway may create an application
and never move one.

**Silently wrong.** Every 404 from Google meant "history expired", so a deleted
message relisted the whole mailbox — and the relist never re-established the
cursor, so once stale it stayed stale for ever. Every 403 meant "sign in again",
including quota errors that signing in cannot fix. The access token was
refreshed once per message ingested. An `.ics` was never unescaped, so every
interview location in the database reads `Dinova\, Via Francesco Zanardi\, 51`.
A correction recorded the wrong before-value for every field but one.

Do not delete the TypeScript version. It is the reference the port is diffed
against and it is what currently reads the mailbox. It retires when P3 passes —
when the existing PWA runs unmodified against the Python API.
