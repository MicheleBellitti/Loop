"""Read messages again, from the provider, and put them back through the chain.

No table stores message text — that is the point of `seen_messages`, which keeps
an id and a hash and nothing else. So a replay is not a database operation: the
bodies have to come back from Gmail, one call each.

Two modes, and the first exists so the second is never a surprise.

    --dry-run   fetch, read, and print what the ladder makes of each message.
                Writes nothing, publishes nothing, costs one Gmail call each.

    (default)   the same fetch, then publish onto `raw_message` and let the six
                services do what they do. `seen_messages` is left alone; the
                classifier will set the outcome when it reaches a verdict.

By default it takes the messages with no verdict at all — the ones that were
recorded as seen and never carried any further.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from loop.connector.normalise import to_raw_message
from loop.db import Database, publish
from loop.db.queue import Queue
from loop.domain.messages import CandidateMessage
from loop.domain.wire import encode_raw_message
from loop.google.client import GoogleClient
from loop.google.mailbox import Mailbox, read_refresh_token, to_mailbox
from loop.ladder import classify
from loop.ladder.ladder import Extracted, Ignored, NeedsReview
from loop.services import ClassifierService, ExtractorService

# Which messages a run is about. Interpolated from a fixed mapping, never from
# the argument itself.
_WHICH = {
    "none": "and outcome is null",
    "all": "",
    "dropped": "and outcome = 'dropped'",
    "review": "and outcome = 'review'",
    "placed": "and outcome = 'placed'",
    "parked": "and outcome = 'parked'",
}

_MAILBOX = """
select id, user_id, provider, address, secret_ciphertext, secret_nonce,
       dek_wrapped, dek_nonce, scopes, cursor, watch_expires_at, status, last_ok_at
  from mailbox_accounts where provider = 'gmail' order by created_at limit 1
"""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What one message came to, and what it had come to before."""

    provider_message_id: str
    received_at: str
    before: str
    score: int
    outcome: str
    intent: str | None
    company: str | None
    role: str | None
    subject: str


async def main(argv: Sequence[str]) -> int:
    options = _parse(argv)
    dsn = os.environ["DATABASE_URL"]

    async with Database(dsn, role=None) as db:
        async with db.untenanted() as connection:
            row = await connection.fetchrow(_MAILBOX)
            if row is None:
                print("no gmail account is connected", file=sys.stderr)
                return 1
            mailbox = to_mailbox(row)
            waiting = await connection.fetch(
                f"""
                select provider_message_id, received_at, outcome
                  from seen_messages
                 where mailbox_id = $1 {_WHICH[options.outcome]}
                   and ($3::date is null or received_at >= $3)
                   and ($4::date is null or received_at < $4)
                 order by {"random()" if options.random else "received_at desc"}
                 limit $2
                """,
                mailbox.id,
                options.limit,
                options.since,
                options.until,
            )

        if not waiting:
            print("nothing to reprocess")
            return 0

        google = GoogleClient(
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        )
        try:
            token = await google.access_token(mailbox.id, read_refresh_token(mailbox))
            if options.dry_run:
                await _report(db, google, token, mailbox, waiting)
            else:
                await _replay(db, google, token, mailbox, waiting)
        finally:
            await google.aclose()
    return 0


async def _report(
    db: Database,
    google: GoogleClient,
    token: str,
    mailbox: Mailbox,
    waiting: Sequence[Any],
) -> None:
    """What the new code makes of each message, beside what it said before."""
    classifier = ClassifierService(db)
    extractor = ExtractorService(db)
    screen_context = await classifier.context_for(mailbox.user_id)
    ladder_context = await extractor.context_for(mailbox.user_id)
    ladder = extractor.ladder

    verdicts: list[Verdict] = []
    for row in waiting:
        raw = await _fetch(google, token, mailbox, row["provider_message_id"])
        if raw is None:
            continue
        screened = classify(raw, screen_context)

        outcome, intent, company, role = "dropped", None, None, None
        if screened.outcome != "drop":
            read = ladder.run(
                CandidateMessage(
                    message=raw,
                    score=screened.score,
                    cheap_only=screened.outcome == "cheap_only",
                    reasons=screened.reasons,
                ),
                ladder_context,
            )
            match read:
                case Extracted(signal):
                    outcome = "extracted"
                    intent, company, role = signal.intent, signal.company, signal.role
                case NeedsReview(_excerpt, read_intent, _confidence):
                    outcome, intent = "review", read_intent
                case Ignored(_reason):
                    outcome = "ignored"

        verdicts.append(
            Verdict(
                provider_message_id=raw.provider_message_id,
                received_at=f"{raw.received_at:%Y-%m-%d}",
                before=row["outcome"] or "—",
                score=screened.score,
                outcome=outcome,
                intent=intent,
                company=company,
                role=role,
                subject=raw.headers.subject[:44],
            )
        )
    _print(verdicts)


def _print(verdicts: Sequence[Verdict]) -> None:
    print(f"\n{len(verdicts)} messages read again\n")
    print(f"{'date':11} {'was':8} {'now':10} {'sc':>3}  {'intent':20} {'company':18} subject")
    print("─" * 118)
    for v in verdicts:
        print(
            f"{v.received_at:11} {v.before:8} {v.outcome:10} {v.score:>3}  "
            f"{(v.intent or '—'):20} {(v.company or '—')[:18]:18} {v.subject}"
        )

    changed = [v for v in verdicts if v.outcome in ("extracted", "review")]
    print(f"\n{'':11} {len(changed)} of {len(verdicts)} now carry a reading")
    for outcome in ("extracted", "review", "dropped", "ignored"):
        count = sum(1 for v in verdicts if v.outcome == outcome)
        if count:
            print(f"{'':13} {outcome:10} {count}")


async def _replay(
    db: Database,
    google: GoogleClient,
    token: str,
    mailbox: Mailbox,
    waiting: Sequence[Any],
) -> None:
    """Back onto the first queue, in batches, and the services take it from there."""
    published = skipped = 0
    for row in waiting:
        raw = await _fetch(google, token, mailbox, row["provider_message_id"])
        if raw is None:
            skipped += 1
            continue
        async with db.session(mailbox.user_id) as connection:
            await publish(connection, Queue.RAW, encode_raw_message(raw))
        published += 1
        if published % 100 == 0:
            print(f"  {published} queued", flush=True)
    print(f"{published} messages queued, {skipped} could not be read")


async def _fetch(
    google: GoogleClient, token: str, mailbox: Mailbox, message_id: str
) -> Any:
    """One message, with its invitation hydrated. None when it is gone.

    A message deleted since it was first seen is not an error: the replay log
    outlives the mailbox, which is the whole reason it can be replayed at all.
    """
    try:
        message = await google.hydrate_calendar_parts(
            token, await google.get_message(token, message_id)
        )
    except Exception as error:
        print(f"  could not read {message_id}: {error}", file=sys.stderr)
        return None
    return to_raw_message(
        message, user_id=mailbox.user_id, mailbox_id=mailbox.id, backfill=True
    )


def _day(value: str) -> date:
    """A real date, because asyncpg binds the column's type and not a string."""
    return date.fromisoformat(value)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument(
        "--outcome",
        default="none",
        choices=sorted(_WHICH),
        help="which messages to take; 'none' means the ones with no verdict",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since", type=_day, default=None, help="YYYY-MM-DD")
    parser.add_argument("--until", type=_day, default=None, help="YYYY-MM-DD, exclusive")
    parser.add_argument(
        "--random",
        action="store_true",
        help="sample across the whole window rather than taking the newest; "
        "the newest of a year-long backfill are all from its last week",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
