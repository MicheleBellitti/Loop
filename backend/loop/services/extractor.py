"""The ladder, as a service.

Three lines of this file are the reason the port exists. The ladder runs between
two short transactions and holds no connection while it does — so when rung 3
becomes a model call taking seconds on a GPU, nothing about this shell changes.
The reference ran the same call inside the transaction that had claimed the
queue item, and Postgres eventually terminated the connection mid-inference.

The four outcomes are all terminal here, and only one of them produces a signal:

    Extracted           → publish, and let the resolver decide what it is about
    NeedsReview         → one review item, once, with the evidence attached
    Ignored             → recorded, no question asked, nothing for a human to do
    TransientRungError  → parked, to be brought back by the drain
"""

import asyncio
import logging
from dataclasses import dataclass

import asyncpg

from loop.db import Database, Message, publish
from loop.db.queue import Queue
from loop.db.seen import Outcome, mark_seen, repark
from loop.domain.messages import CandidateMessage, Intent
from loop.domain.types import Rung
from loop.domain.wire import Json, decode_candidate_message, encode_signal
from loop.ladder import (
    Extracted,
    Ignored,
    Ladder,
    LadderContext,
    ModelConfig,
    NeedsReview,
    RuleRegistry,
    TransientRungError,
    model_ladder,
)

from .consumer import Consumer, ConsumerOptions

__all__ = ["ExtractorService", "Reading", "TransientRungError"]


@dataclass(frozen=True, slots=True)
class Reading:
    """What one message came to, so a caller can assert on it."""

    outcome: str
    intent: Intent | None = None
    rung: Rung | None = None
    confidence: float = 0.0


class ExtractorService:
    def __init__(
        self,
        db: Database,
        *,
        registry: RuleRegistry | None = None,
        ladder: Ladder | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._db = db
        self._registry = registry or RuleRegistry.load()
        # All three rungs. The third abstains on its own line when
        # `MODEL_BASE_URL` is unset, which is the default posture — so this is
        # the deterministic ladder until someone configures a model, and
        # nothing here moves when they do.
        self._ladder = ladder or model_ladder(ModelConfig.from_env())
        self._log = log or logging.getLogger("loop.extractor")

    @property
    def ladder(self) -> Ladder:
        """For a replay, which needs to read a message without recording it."""
        return self._ladder

    def consumer(self, options: ConsumerOptions | None = None) -> Consumer:
        # Four: small enough that a batch of model calls is not an hour of
        # claimed work, and the lease is refreshed per message either way.
        return Consumer(
            self._db,
            Queue.CANDIDATE,
            self.handle,
            options=options or ConsumerOptions(batch=4),
            log=self._log,
        )

    async def handle(self, message: Message) -> None:
        body = message.body
        if _is_replay_stub(body):
            await self._absorb_replay_stub(body)
            return
        await self.extract(decode_candidate_message(body))

    async def extract(self, msg: CandidateMessage) -> Reading:
        user_id = msg.message.user_id
        context = await self.context_for(user_id)

        try:
            # Off the loop, not off the transaction — the transaction is
            # already closed. Rung 3 blocks for as long as an inference takes,
            # and a process whose only loop is stalled cannot answer a SIGTERM
            # or renew the lease on the message it is holding. Rungs 1 and 2
            # pay a thread hop measured in microseconds for the privilege.
            outcome = await asyncio.to_thread(self._ladder.run, msg, context)
        except TransientRungError as error:
            return await self._park(msg, error)

        match outcome:
            case Extracted(signal):
                async with self._db.session(user_id) as connection:
                    await publish(connection, Queue.SIGNAL, encode_signal(signal))
                # `seen_messages` stays open deliberately: the resolver owns the
                # terminal verdict, and closing it here would mark a message
                # finished before it had been placed anywhere.
                reading = Reading("extracted", signal.intent, signal.rung, signal.confidence)
            case NeedsReview(excerpt, intent, confidence):
                await self._ask_a_human(msg, excerpt)
                reading = Reading("review", intent, None, confidence)
            case Ignored(reason):
                async with self._db.session(user_id) as connection:
                    await self._close(connection, msg, "dropped")
                self._log.info("ignored %s: %s", msg.message.provider_message_id, reason)
                reading = Reading("ignored")

        self._log.info(
            "%s %s intent=%s rung=%s confidence=%.2f",
            reading.outcome,
            msg.message.provider_message_id,
            reading.intent,
            reading.rung,
            reading.confidence,
        )
        return reading

    async def _ask_a_human(self, msg: CandidateMessage, excerpt: str) -> None:
        """Rung 4. Once per message, not once per delivery.

        The reference wrote `on conflict do nothing`, which on this table is a
        no-op — its only unique constraint is a generated primary key, so there
        is nothing for a conflict to arc across and a redelivered message raised
        the same question twice.
        """
        async with self._db.session(msg.message.user_id) as connection:
            await connection.execute(
                """
                insert into review_items (user_id, kind, evidence_ref, excerpt)
                select $1, 'unknown_intent', $2, $3
                 where not exists (
                   select 1 from review_items
                    where user_id = $1 and kind = 'unknown_intent'
                      and evidence_ref = $2 and resolved_at is null)
                """,
                msg.message.user_id,
                msg.message.provider_message_id,
                excerpt,
            )
            await self._close(connection, msg, "review")

    async def _park(self, msg: CandidateMessage, error: TransientRungError) -> Reading:
        """Not now. `drain_parked` brings it back every fifteen minutes, and
        turns it into a review item once it has tried six times."""
        async with self._db.session(msg.message.user_id) as connection:
            await self._close(connection, msg, "parked")
        self._log.warning("parked %s: %s", msg.message.provider_message_id, error.kind)
        return Reading("parked")

    async def _absorb_replay_stub(self, body: Json) -> None:
        """What the drain puts back on the queue is not a message.

        `drain_parked` enqueues four keys — no headers, no body — because the
        body was deleted with the queue row at acknowledgement and no table
        stores message text. There is nothing here to run the ladder on, so the
        honest handling is to put the row back where the drain found it and let
        the drain's own attempt counter escalate it to a review item.

        Raising instead would dead-letter the stub, and the row would then be
        invisible to both the drain and the escalation: a message the user was
        promised would come back, silently gone.
        """
        async with self._db.session(body["user_id"]) as connection:
            await repark(connection, body["mailbox_id"], body["provider_message_id"])
        self._log.warning(
            "replay stub for %s carries no body; reparked", body["provider_message_id"]
        )

    async def _close(
        self, connection: asyncpg.Connection, msg: CandidateMessage, outcome: Outcome
    ) -> None:
        await mark_seen(
            connection, msg.message.mailbox_id, msg.message.provider_message_id, outcome
        )

    async def context_for(self, user_id: str) -> LadderContext:
        """Read per message, never cached — and public, so a replay reads it too.

        The thread map comes out of `application_events`, which only the pipeline
        writes, so it already lags what the resolver has decided. Caching it
        widens the window in which two messages on one thread each find nothing
        to inherit from and become two applications — the backfill race in
        `docs/port-to-python.md` §P2. Two indexed reads is not a price worth
        paying for that.
        """
        async with self._db.session(user_id) as connection:
            threads = await connection.fetch(
                """
                select distinct on (payload->>'thread_id')
                       payload->>'thread_id' as thread_id, application_id
                  from application_events
                 where user_id = $1 and payload ? 'thread_id'
                 order by payload->>'thread_id', occurred_at desc, id desc
                """,
                user_id,
            )
            addresses = await connection.fetch(
                "select address from mailbox_accounts where user_id = $1", user_id
            )

        return LadderContext(
            registry=self._registry,
            # `distinct on … order by occurred_at desc` rather than the
            # reference's unordered `distinct`: a thread that has appeared under
            # two applications — after a merge, after a split — resolved there to
            # whichever row Postgres returned last. Here it resolves to the
            # application the thread most recently produced an event for, which
            # is both deterministic and the answer a human would give.
            thread_to_application={
                row["thread_id"]: str(row["application_id"])
                for row in threads
                if row["thread_id"]
            },
            # Every provider, unfiltered: a calendar account's address is still
            # the user. The reference loaded none of these, so the user's own
            # replies were read as the employer's and became review items with
            # no answer.
            own_addresses=frozenset(str(row["address"]).lower() for row in addresses),
        )


def _is_replay_stub(body: Json) -> bool:
    return body.get("replay") is True and "headers" not in body
