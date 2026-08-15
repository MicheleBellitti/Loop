# Runbook

The system is eight small services, one Postgres and a reverse proxy on one box.
Everything downstream of the connector is replayable from `seen_messages`, so
the box can be down for a day and lose nothing but timeliness.

---

## The one question that matters

**"Is it still reading my mail?"**

A silent connector is indistinguishable from a quiet job market, which is the
failure mode this product must never have. `last_ok_at` per mailbox is therefore
a first-class value: it is on `/api/mailboxes`, in the app header, and alerted
on.

| Signal | Threshold | What it means |
| --- | --- | --- |
| `now - last_ok_at` | > 2 h | warn — a watch may have lapsed |
| `now - last_ok_at` | > 12 h | alert — something is actually wrong |
| oldest unprocessed queue item | > 30 min | alert — a consumer is stuck or dead |
| dead-letter count | > 0 | alert — a message failed five times |

```bash
curl -s localhost:3000/health/deep | jq
```

---

## The four failure states, and what to do

**F1 · access revoked** — the only one that needs you. The app shows a
full-screen banner with the last successful read and the applications kept.
Reconnect from that screen. Nothing was lost; missed mail is caught up on the
next sync.

**F2 · watch lapsed** — self-healing. A strip reads "Catching up · N messages
behind" and the backlog drains. No action.

**F3 · IMAP login refused** — not built. IMAP ships when a second provider does
(decisions.md OPEN-2); this state has no trigger until then.

**F4 · model offline** — self-healing, and the default posture. Template rules
and calendar detection keep running; unknown templates are parked, re-published
every 15 minutes, and become review items after six attempts. The per-component
status is on the dashboard and in `/health/deep`.

---

## Common operations

### Run compose at all

```bash
cd infra && docker compose --env-file ../.env build
cd infra && docker compose --env-file ../.env up -d
```

Every `docker compose` line below assumes both: run from `infra/`, and pass
`--env-file ../.env`. The flag is what supplies `POSTGRES_PASSWORD`, and
`compose.yaml` refuses to parse without it — `env_file: ../.env` covers what a
container sees at run time, not what this file interpolates at parse time, and
interpolation looks only in `infra/`. `POSTGRES_PASSWORD` must also be
non-empty: `${POSTGRES_PASSWORD:?…}` counts an empty value as missing, so a
`.env` copied from `.env.example` and left unedited fails the same way as no
`.env` at all.

### Apply migrations

```bash
cd backend && uv run python -m loop migrate
```

Refuses to run a migration whose contents changed after it was applied. If you
need to alter something, write a new numbered file — that is what
`007_refresh_grant.sql` is.

### Rebuild the projection from the log

`applications` is derived. If a fold change means last month's history should be
re-derived:

```bash
cd backend && USER_ID=… uv run --extra db python -c "
import asyncio, os
from loop.db import Database, rebuild_all

async def main():
    user = os.environ['USER_ID']
    async with Database(os.environ['DATABASE_URL'], role='loop_pipeline') as db:
        async with db.session(user) as connection:
            print(await rebuild_all(connection, user), 'rebuilt')

asyncio.run(main())"
```

It blanks every derived column and re-folds, so a rebuild that produced the same
row by accident — because the old value was still sitting there — does not pass.
`tests/test_rebuild.py` asserts the result is byte-identical, so a difference
after a rebuild means the fold changed, which is sometimes exactly what you
wanted.

### Rotate the KEK

Every DEK is re-wrapped in one transaction. The KEK is never in a backup, so a
restore means reconnecting mailboxes — that is by design and it is stated here
so it is not a surprise.

```bash
cd backend && LOOP_KEK_OLD=… LOOP_KEK=… \
  uv run --extra db --extra connector python scripts/rotate_kek.py
```

One transaction with `for update`, so it is every data key or none of them: a
rotation that half-finished would leave some mailboxes readable under the old
key and some under the new one, with no record of which.

### Back up

```bash
infra/backup.sh
```

`pg_dump`, encrypted with `age`, to object storage, 30-day rotation. **The KEK
is not in the backup.** Restoring gives you every application and every event;
the mailboxes have to be reconnected.

### Drain a stuck queue

```sql
select queue, count(*), min(enqueued_at) from mq.messages group by queue;
select * from mq.messages where queue like '%_dlq' order by enqueued_at desc limit 20;
```

Dead-lettered payloads have their message text stripped. Replay from the
provider instead:

```sql
update seen_messages set outcome = 'parked', park_attempts = 0
 where provider_message_id = '...';
select drain_parked();
```

### Stop the model

```bash
docker compose --profile model stop llama
```

The extractor abstains, unknown templates go to review, and the user sees F4.
Nothing is lost.

### Run a backfill through vLLM

Continuous batching turns a 12-month first scan from hours into minutes:

```bash
docker compose --profile batch up -d vllm
MODEL_BASE_URL=http://vllm:8000/v1 docker compose up -d extractor
# …then put it back
docker compose --profile batch down
```

---

## What is deliberately absent

- **No send path.** Not SMTP, not the Gmail send API, not a hosted mail
  provider. `backend/scripts/assert_no_send_path.py` fails the build if one
  appears — over every `.py`, `.ts`, `.sql` and `.yaml` in the tree.
- **No message bodies in any table.** `review_items.excerpt` is the single
  exception: ≤280 characters, redacted, deleted with the item.
- **No hosted model by default.** `ALLOW_HOSTED_MODEL` must be explicitly true,
  and the config loader refuses a `MODEL_BASE_URL` that points off the box
  without it.
- **No portal scraping.** Credential custody, CAPTCHA, ToS risk and constant
  breakage, for a status field that lags the email by days.

---

## Deleting an account

```bash
curl -X DELETE localhost:3000/api/account \
  -H 'content-type: application/json' -H "x-csrf-token: $CSRF" \
  --cookie "loop_session=$SESSION" -d '{"confirm":"DELETE"}'
```

Cascade, plus the queue purge, plus the OAuth grant revoked at Google. Returns a
receipt id. An integration test asserts every table is empty for that user
afterwards, including the queue — an erasure that leaves your mail sitting in a
queue is not an erasure.
