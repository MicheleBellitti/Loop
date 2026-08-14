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
node -e "console.log('LOOP_KEK=' + require('crypto').randomBytes(32).toString('base64'))" >> .env
npm install
npm run test:db:up          # Postgres 16 + pgvector + pg_cron, on :55432
npm run migrate
npm run seed:user -- you@example.com
```

Then, in two terminals:

```bash
npm run dev
```

```bash
npm run client:dev
```

The app is at http://localhost:5173. Sign in with the recovery password the seed
printed, add a passkey, and connect a mailbox — or skip the mailbox and add
applications by hand until you have a Google client (see
[docs/google-setup.md](docs/google-setup.md)).

### Without a mailbox

You can watch the whole pipeline run against the golden corpus, with a stub
standing in for Google and nothing else faked:

```bash
npm run e2e
```

---

## What the commands do

| Command | What it does |
| --- | --- |
| `npm test` | Unit tests. Pure, no I/O, run everywhere. |
| `npm run test:integration` | Against a real Postgres: RLS, append-only, idempotency, rebuild, erasure. |
| `npm run test:corpus` | The confusion matrix, with the §17 merge gates enforced. |
| `npm run lint:no-send-path` | Asserts no SMTP or Gmail send API is reachable from anywhere in the tree. |
| `npm run fixtures` | Regenerates the synthetic corpus. |
| `npm run anonymise -- <dir>` | Turns your own mail into fixtures, locally, into a git-ignored directory. |
| `npm run e2e` | The full pipeline against a stub mailbox. |
| `npm run migrate` | Applies migrations. Refuses to run one that changed after it was applied. |

---

## Layout

```
packages/
  domain/     types, stage machine, the fold, thresholds — pure, 100% unit-tested
  db/         migrations, RLS helpers, envelope encryption, the projection
  queue/      the queue wrapper: publish, consume, retry, dead-letter
  rules/      the ATS template registry and its matcher
  google/     the Gmail and Calendar client, and mailbox secret handling
  runtime/    config, structured logging, metrics, service bootstrap
services/
  gateway/    Fastify: REST + SSE, sessions, serves the built client
  connector/  Gmail watch + history sync, backfill, Calendar sync
  classifier/ is-this-about-a-job — drops ~95% of an inbox
  extractor/  rungs 1–3, the rule registry, the model adapter
  resolver/   entity resolution, dedup, the review queue
  pipeline/   event append + projection fold — the only writer
  nudge/      the four rules and the budget
  notifier/   web push
client/       the PWA: mobile, desktop dashboard, onboarding
rules/ats/    *.yaml templates — data, reviewed like code
fixtures/     the golden corpus, plus negatives that must be dropped
infra/        compose.yaml, Caddyfile, the Postgres image, backups
docs/         decisions.md — read this before changing anything normative
design/       the original handoff bundle, unmodified
```

---

## The parts worth knowing before you change something

**The fold is not what the spec says it is.** Engineering Spec §05 orders events
by confidence; that rule cannot advance an application past its own ATS
auto-reply, and the bundle's worked example proves it. The implemented rule is
recency-first with a human pin. [docs/decisions.md](docs/decisions.md) §A1 has
the full argument, and `packages/domain/src/fold.test.ts` has the case.

**There is no send path.** Loop drafts a follow-up and opens your mail client.
Nothing in this repository can deliver a message, `npm run lint:no-send-path`
enforces that, and the weekly digest is a page plus a push rather than an email
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
zero wrong merges. `npm run anonymise` and `npm run test:corpus` are how you
measure it. If extraction is not accurate enough to trust, no amount of
interface work saves the product.
