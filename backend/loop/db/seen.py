"""The replay log: what happened to every message this mailbox has shown us.

One row per message, written by the connector and closed by whichever stage
reached a verdict. It is what makes the box safe to be down for a day, and it is
the only way a false negative is ever found — a message that was dropped is
recorded as dropped rather than forgotten.

The update below is worth its own module for one reason. Under row-level
security an update whose predicate matches nothing is `UPDATE 0`, not an error,
so a tenanting bug and a working system look identical from the caller's side.
Reading the command tag is the difference, and doing it in one place means every
service does it.
"""

import logging
from typing import Literal

import asyncpg

# The four verdicts a message can end on. A comment in migration 002 rather than
# a CHECK constraint, so nothing but this type stops a typo.
Outcome = Literal["placed", "dropped", "parked", "review"]

_log = logging.getLogger("loop.db.seen")


async def mark_seen(
    connection: asyncpg.Connection,
    mailbox_id: str,
    provider_message_id: str,
    outcome: Outcome,
) -> bool:
    """Close a message out. False when there was no row to close.

    A missing row means the connector never recorded this message — replaying it
    forever would not create one, so the caller carries on and the warning is
    what surfaces it.
    """
    tag = await connection.execute(
        """
        update seen_messages set outcome = $3, processed_at = now()
         where mailbox_id = $1 and provider_message_id = $2
        """,
        mailbox_id,
        provider_message_id,
        outcome,
    )
    affected = _rows(tag)
    if not affected:
        _log.warning("no seen_messages row for %s/%s", mailbox_id, provider_message_id)
    return bool(affected)


async def repark(
    connection: asyncpg.Connection, mailbox_id: str, provider_message_id: str
) -> bool:
    """Put a message the drain took back where it found it.

    `outcome is null` is the guard that matters: the drain clears the outcome
    before re-queuing, and between then and now the connector may have re-read
    the message and the resolver placed it. Reparking that row would undo real
    work.
    """
    tag = await connection.execute(
        """
        update seen_messages set outcome = 'parked'
         where mailbox_id = $1 and provider_message_id = $2 and outcome is null
        """,
        mailbox_id,
        provider_message_id,
    )
    return bool(_rows(tag))


def _rows(tag: str) -> int:
    """asyncpg returns the command tag verbatim: `UPDATE 3`."""
    _, _, count = tag.rpartition(" ")
    return int(count) if count.isdigit() else 0
