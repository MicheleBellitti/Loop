"""The queue: five hops, one Postgres, no Redis.

`mq.read` claims messages with SKIP LOCKED and hides them for a visibility
timeout, committing as it goes. That is the detail the reference did not exploit:
because the claim is already durable, **the handler does not need to hold a
transaction**, and here it does not.

    claim a batch          → one short transaction, commits
    run the work           → no transaction, no connection held
    append and acknowledge → one short transaction, commits

The TypeScript ran the handler inside the claiming transaction, so a rung-3
inference left a connection idle-in-transaction for the length of the call;
Postgres eventually terminated it mid-flight and the unhandled error took the
process down. The fix there was to raise `idle_in_transaction_session_timeout`
to three minutes, which couples two numbers that have nothing to do with each
other. With in-process inference on a GPU it would have been worse: a pool of
ten exhausted by ten concurrent calls.

What makes the third step safe to retry after a crash in the second is the
unique index on `(application_id, type, occurred_at, evidence_ref)`. A message
redelivered after its visibility timeout appends nothing the second time.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import asyncpg

from loop.domain.messages import strip_bodies
from loop.domain.thresholds import MAX_ATTEMPTS, VISIBILITY_TIMEOUT_SECONDS

from .pool import Database


class Queue:
    """One of the five hops, by name."""

    RAW: Final = "raw_message"
    CANDIDATE: Final = "candidate_message"
    SIGNAL: Final = "signal_extracted"
    EVENT: Final = "event_pending"
    NOTIFY: Final = "notify_pending"

    ALL: Final = (RAW, CANDIDATE, SIGNAL, EVENT, NOTIFY)


@dataclass(frozen=True, slots=True)
class Message:
    msg_id: int
    body: dict[str, Any]
    # How many times this has been claimed, including now. A handler that keeps
    # failing is dead-lettered rather than retried forever.
    read_count: int

    @property
    def exhausted(self) -> bool:
        return self.read_count > MAX_ATTEMPTS


async def publish(
    connection: asyncpg.Connection, queue: str, body: dict[str, Any], *, delay: int = 0
) -> int:
    row = await connection.fetchval("select mq.send($1, $2, $3)", queue, body, delay)
    return int(row)


async def publish_many(
    connection: asyncpg.Connection, queue: str, bodies: Sequence[dict[str, Any]]
) -> int:
    """A backfill enqueues thousands at a time; one round trip, not thousands."""
    if not bodies:
        return 0
    rows = await connection.fetch("select mq.send_batch($1, $2)", queue, list(bodies))
    return len(rows)


async def claim(
    db: Database,
    queue: str,
    *,
    batch: int = 10,
    visibility: int = VISIBILITY_TIMEOUT_SECONDS,
) -> list[Message]:
    """Take up to `batch` messages and hide them for `visibility` seconds.

    Returns as soon as the claim commits. Whatever the caller does next happens
    with no connection held.
    """
    async with db.untenanted() as connection:
        rows = await connection.fetch(
            "select * from mq.read($1, $2, $3)", queue, visibility, batch
        )
    # No defensive decode here on purpose: the codec returns a mapping, and a
    # `json.loads` fallback would have quietly absorbed a double-encoded write
    # rather than failing on it — which is exactly what it did.
    return [
        Message(msg_id=row["msg_id"], body=row["message"], read_count=row["read_ct"])
        for row in rows
    ]


async def acknowledge(db: Database, queue: str, msg_id: int) -> bool:
    async with db.untenanted() as connection:
        return bool(await connection.fetchval("select mq.delete($1, $2)", queue, msg_id))


async def dead_letter(db: Database, queue: str, message: Message) -> None:
    """Park a message that has failed too often, without its body.

    §05 asks for the original payload and §04 says no table ever stores message
    text. Both hold: the text travels in the queue only while a message is in
    flight, and the acknowledgement deletes the row. A dead letter is the one
    payload that would persist indefinitely, so the body comes off — and nothing
    is lost, because `seen_messages` makes every message replayable from the
    provider by id once the bug is fixed.
    """
    stripped = strip_bodies(message.body)
    async with db.untenanted() as connection, connection.transaction():
        await connection.execute("select mq.send($1, $2)", f"{queue}_dlq", stripped)
        await connection.execute("select mq.delete($1, $2)", queue, message.msg_id)


async def depth(connection: asyncpg.Connection, queue: str) -> int:
    row = await connection.fetchrow("select * from mq.metrics($1)", queue)
    return int(row["queue_length"]) if row else 0


async def dead_letter_depth(connection: asyncpg.Connection) -> int:
    """The §16 alert fires on any dead letter at all, so this is a sum."""
    total = 0
    for queue in Queue.ALL:
        total += await depth(connection, f"{queue}_dlq")
    return total
