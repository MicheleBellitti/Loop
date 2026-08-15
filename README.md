# Loop

A job application tracker you never update by hand.

Your mailbox already knows every application you sent, every auto-acknowledgement,
every invitation and every rejection — it is just unstructured. Loop reads those
signals, resolves them into one application per real job, and derives the
pipeline instead of asking you to maintain it. You intervene for roughly 1% of
messages, in a two-tap review queue.

Self-hosted, single-tenant, multi-tenant-ready. Email and calendar are the only
automated sources; manual add is the fallback. Running cost target: €0.

---

## Getting it running

```bash
cp .env.example .env
python3 -c "import os,base64;print('LOOP_KEK='+base64.b64encode(os.urandom(32)).decode())" >> .env
python3 -c "import secrets;print('SESSION_SECRET='+secrets.token_urlsafe(32))" >> .env

cd backend
uv sync --extra api --extra db --extra connector --extra push --extra ladder
uv run python scripts/test_db.py up     # Postgres 16 + pgvector + pg_cron, :55432
uv run python -m loop migrate
uv run python scripts/seed_user.py you@example.com
```

Then, in two terminals:

```bash
cd backend && uv run python scripts/dev.py
```

```bash
cd frontend && npm install && npm run dev
```

The app is at http://localhost:5173. Sign in with the recovery password the seed
printed, add a passkey, and connect a mailbox — or skip the mailbox and add
applications by hand until you have a Google client (see
[docs/google-setup.md](docs/google-setup.md)).

### Without a mailbox

You can watch the whole pipeline run against the golden corpus, with a stub
standing in for Google and nothing else faked:

```bash
cd backend && uv run python scripts/e2e.py
```

---

## What the commands do

Everything below runs from `backend/`, under `uv run`.

| Command | What it does |
| --- | --- |
| `pytest` | The lot. Pure tests everywhere; the integration ones skip without `DATABASE_URL`. |
| `pytest -m "not integration"` | Pure only. No I/O, no database, under a second. |
| `python scripts/corpus_gate.py` | The confusion matrix, with the §17 merge gates enforced. |
| `python scripts/assert_no_send_path.py` | Asserts no SMTP or Gmail send API is reachable from anywhere in the tree. |
| `python scripts/gen_fixtures.py` | Regenerates the synthetic corpus. `--check` asserts it has not drifted. |
| `python scripts/anonymise.py <dir>` | Turns your own mail into fixtures, locally, into a git-ignored directory. |
| `python scripts/e2e.py` | The full pipeline against a stub mailbox. |
| `python -m loop migrate` | Applies migrations. Refuses to run one that changed after it was applied. |
| `python scripts/seed_user.py <email>` | Creates the single user and prints a recovery password once. |
| `python scripts/rotate_kek.py` | Re-wraps every data key under a new `LOOP_KEK`. |
| `python scripts/test_db.py up\|down` | The integration-test Postgres, on :55432. |
| `python scripts/dev.py` | All eight processes in one terminal, each with its own database role. |
| `ruff check .` · `mypy loop` | The two static gates. |

And from `frontend/`: `npm run dev`, `npm run build`, `npm run typecheck`.

---

## Layout

```
backend/
  loop/
    domain/     the fold, stages, thresholds, nudges, wire codecs — pure
    ladder/     classifier, the rule registry, rungs 1–3, the signal
    resolver/   embedder, matching, company identity, intent → event — pure
    connector/  a Gmail message and an .ics, read — pure
    google/     the API client, the sealed secret, the mailbox row
    db/         asyncpg, the tenant session, the queue, migrations, rebuild
    services/   the eight long-running processes
    api/        FastAPI: REST + SSE, sessions, serves the built client
    runtime/    the redacting log
    harness/    the corpus, the runner, the divergence table
  migrations/   numbered .sql — schema, RLS, grants, the queue, the sweeps
  rules/ats/    *.yaml templates — data, reviewed like code
  fixtures/     the golden corpus, plus negatives that must be dropped
  scripts/      seed, migrate, replay, e2e, the corpus gate, the stub Google
  tests/
frontend/       the PWA: mobile, desktop dashboard, onboarding
infra/          compose.yaml, Caddyfile, the Postgres image, backups
docs/           decisions.md — read this before changing anything normative
design/         the original handoff bundle, unmodified
```

`loop.domain` imports nothing outside the standard library, which is why its
tests run in a tenth of a second. `loop.ladder` adds PyYAML, httpx and Pydantic;
nothing depends on the harness. The eight processes each connect as their own
database role, so the grants in `migrations/003_rls.sql` are the enforcement of
"only the pipeline writes to the event log" rather than a convention.

---

## The parts worth knowing before you change something

**The fold is not what the spec says it is.** Engineering Spec §05 orders events
by confidence; that rule cannot advance an application past its own ATS
auto-reply, and the bundle's worked example proves it. The implemented rule is
recency-first with a human pin. [docs/decisions.md](docs/decisions.md) §A1 has
the full argument, and `backend/tests/test_fold.py` has the case.

**There is no send path.** Loop drafts a follow-up and opens your mail client.
Nothing in this repository can deliver a message,
`backend/scripts/assert_no_send_path.py` enforces that, and the weekly digest is a page plus a push rather than an email
for the same reason.

**Every displayed figure carries its denominator.** A ratio without its
numerator, denominator and exclusion count is a bug, not a styling choice.
Below its gate a figure is withheld and the gate is named.

**`status` is not the same question as "is this still happening".** An
application is `live` until something closes it, and on a twelve-month mailbox
most processes end by simply stopping — so `live` counted fourteen where the
truth was four, and held every ratio's gate shut behind it. `activity` is the
answer to the second question, derived on every read from silence, stage and
the calendar rather than from a nightly sweep having run. The board, the
counters and the statistics all use it, and
[docs/decisions.md](docs/decisions.md) §F1 is the argument.

**Provenance is on every automated claim.** Source and confidence, on every
event row. It is the mechanism by which you learn when to trust the thing.

**Rung 3 is off by default.** Unknown templates become review items — which is
failure state F4, so F4 is the development posture and gets exercised on every
run rather than only in a test. Turn the model on with `MODEL_BASE_URL`.

---

## The order to build in

Phase 1 is the project's go/no-go and it is not the interface: read twelve
months of your own inbox and measure. Target ≥0.85 application-level recall with
zero wrong merges. `scripts/anonymise.py` and `scripts/corpus_gate.py` are how
you measure it. If extraction is not accurate enough to trust, no amount of
interface work saves the product.
