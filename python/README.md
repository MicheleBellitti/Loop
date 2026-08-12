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

## Running it

    uv sync --group dev
    uv run pytest
    uv run ruff check .

The core has no runtime dependencies. `loop.domain` imports nothing outside the
standard library, which is why its 87 tests run in under a tenth of a second.

## State

| Phase | | |
|---|---|---|
| P0 | schema and domain, headless | **done** |
| P1 | the ladder and the differential harness | next |
| P2 | connector, resolver, pipeline | |
| P3 | FastAPI, response contract byte-identical | |
| P4 | real embeddings, spaCy, the model out of the transaction | |
| P5 | the interface | |

Do not delete the TypeScript version. It is the reference the port is diffed
against and it is what currently reads the mailbox. It retires when P3 passes —
when the existing PWA runs unmodified against the Python API.
