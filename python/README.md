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

    loop/domain/     the fold, stages, thresholds, nudges, preprocessing — pure
    loop/ladder/     classifier, rule registry, rungs 1 and 2, the signal
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
| P2 | connector, resolver, pipeline | in progress: the resolver's decisions are ported |
| P3 | FastAPI, response contract byte-identical | |
| P4 | real embeddings, spaCy, the model out of the transaction | |
| P5 | the interface | |

Do not delete the TypeScript version. It is the reference the port is diffed
against and it is what currently reads the mailbox. It retires when P3 passes —
when the existing PWA runs unmodified against the Python API.
