"""The pipeline: the only writer of application state.

Concurrency 1, and the database backs that up with grants — no other role holds
INSERT on `application_events`, so this being imported elsewhere fails loudly
rather than quietly corrupting state.

Everything here is idempotent. The unique index on
`(application_id, type, occurred_at, evidence_ref)` means the same queue message
delivered twice produces one row, one projection and one notification.
"""

import asyncio
import json
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import asyncpg

from loop.db import (
    Database,
    Message,
    append_event,
    apply_side_effects,
    project_application,
    refresh_projections,
    to_domain_event,
)
from loop.domain.messages import PendingEvent
from loop.domain.types import DomainEvent
from loop.domain.wire import decode_pending_event

from .consumer import Consumer, ConsumerOptions

# Browsers hold their SSE connection on the gateway, which may be a different
# container from this one. LISTEN/NOTIFY is how a write here reaches a tab
# attached there.
EVENT_CHANNEL = "loop_events"

# `app_phase_reach` is a materialised view, and refreshing it per event would
# make a twelve-month backfill thousands of concurrent refreshes of a table that
# has just changed by one row. Debounced on the trailing edge, so a burst pays
# for one refresh and a single event still gets one five seconds later.
REFRESH_DEBOUNCE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Applied:
    """What one message did, so a caller can assert on it."""

    event_id: str | None
    duplicate: bool
    notified: bool


class PipelineService:
    def __init__(self, db: Database, log: logging.Logger | None = None) -> None:
        self._db = db
        self._log = log or logging.getLogger("loop.pipeline")
        self._projection_is_stale = asyncio.Event()

    def consumer(self, queue: str, options: ConsumerOptions | None = None) -> Consumer:
        return Consumer(self._db, queue, self.handle, options=options, log=self._log)

    def service(
        self,
        queue: str,
        options: ConsumerOptions | None = None,
        *,
        debounce: float = REFRESH_DEBOUNCE_SECONDS,
    ) -> "_PipelineRuntime":
        """The consumer, plus the thing that keeps the funnel's view current.

        Two parts because they stop differently: the consumer finishes the
        message in its hand, and only then is the last refresh worth doing.
        """
        return _PipelineRuntime(self, self.consumer(queue, options), debounce=debounce)

    async def handle(self, message: Message) -> None:
        await self.apply(decode_pending_event(message.body))

    async def apply(self, pending: PendingEvent) -> Applied:
        """Append one event and bring everything derived from it up to date.

        One transaction, deliberately: the event, its satellite rows and the
        projection either all land or none do. It is short because the work that
        is not short — reading a mailbox, asking a model — happened before this
        was ever enqueued.
        """
        async with self._db.session(pending.user_id) as connection:
            event_id = await append_event(connection, pending)
            duplicate = event_id is None

            if not duplicate and pending.source is not None:
                await self._record_source(connection, pending)

            if event_id is not None:
                stored = await self._stored_event(connection, event_id)
                if stored is not None:
                    await apply_side_effects(
                        connection,
                        pending.user_id,
                        pending.application_id,
                        stored,
                        event_id=event_id,
                    )

            # Projected even for a duplicate. It is derived, idempotent and
            # cheap, and skipping it means a redelivery that follows a crash
            # between the append and the projection leaves the row stale
            # forever — the reference returned early here and had that hole.
            await project_application(connection, pending.user_id, pending.application_id)

        # The row is current; the view over every row is not. Signalled rather
        # than refreshed here so the write transaction is not held open for it.
        self._projection_is_stale.set()

        notified = not duplicate and not pending.silent
        if notified:
            await self._notify(pending)

        self._log.info(
            "%s %s for %s",
            "duplicate" if duplicate else "appended",
            pending.type,
            pending.application_id,
        )
        return Applied(event_id=event_id, duplicate=duplicate, notified=notified)

    async def _record_source(
        self, connection: asyncpg.Connection, pending: PendingEvent
    ) -> None:
        """Where this application was found.

        Exactly one first touch per application, enforced by a partial unique
        index — every channel statistic depends on it, so the claim is made
        conditional rather than attempted and caught.
        """
        source = pending.source
        if source is None:
            return
        await connection.execute(
            """
            insert into sources
              (user_id, application_id, channel, posting_url, ats_vendor, is_first_touch)
            values ($1,$2,$3,$4,$5, $6 and not exists (
              select 1 from sources where application_id = $2 and is_first_touch
            ))
            """,
            pending.user_id,
            pending.application_id,
            source.channel,
            source.posting_url,
            source.ats_vendor,
            source.is_first_touch,
        )

    async def _stored_event(
        self, connection: asyncpg.Connection, event_id: str
    ) -> DomainEvent | None:
        """The row as the database holds it.

        Side effects read the stored form rather than the pending one, because
        that is the shape they will meet on every replay: timestamps as the ISO
        strings `jsonb` keeps, ids as the database assigned them. Reading the
        pending form here would let a bug through that only appears the second
        time a message arrives.
        """
        row = await connection.fetchrow(
            "select * from application_events where id = $1", int(event_id)
        )
        return to_domain_event(row) if row else None

    async def _notify(self, pending: PendingEvent) -> None:
        async with self._db.untenanted() as connection:
            await connection.execute(
                "select pg_notify($1, $2)",
                EVENT_CHANNEL,
                json.dumps(
                    {
                        "type": "application.changed",
                        "user_id": pending.user_id,
                        "application_id": pending.application_id,
                    }
                ),
            )

    async def refresh_the_view(self) -> None:
        """The materialised view behind every phase-reach ratio.

        Its own connection, and outside any transaction the writer holds: a
        concurrent refresh of a view is not work to do while the row it
        summarises is still being written.
        """
        async with self._db.untenanted() as connection:
            await refresh_projections(connection)


class _PipelineRuntime:
    """The consumer and the refresher, stopped in that order.

    The refresher is a trailing-edge debounce and nothing else: it waits to be
    told the projection is stale, waits again for the burst to finish, and
    refreshes once. A refresh that fails is logged and retried on the next
    event — the view is a summary, and a stale summary is a worse answer rather
    than a wrong one, which is not a reason to take the writer down.
    """

    def __init__(
        self, service: PipelineService, consumer: Consumer, *, debounce: float
    ) -> None:
        self._service = service
        self._consumer = consumer
        self._debounce = debounce
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        await asyncio.gather(self._consumer.run(), self._refresh_when_stale())

    async def stop(self) -> None:
        await self._consumer.stop()
        self._stopping.set()
        # The consumer has finished the message in its hand, so this is the one
        # refresh that covers everything the process ever applied.
        if self._service._projection_is_stale.is_set():
            await self._refresh_once()

    async def _refresh_when_stale(self) -> None:
        stale = self._service._projection_is_stale
        while not self._stopping.is_set():
            await self._either(stale.wait())
            if self._stopping.is_set():
                return
            # Let the burst finish. Everything that arrives in the window is
            # covered by the one refresh at the end of it.
            await self._either(asyncio.sleep(self._debounce))
            stale.clear()
            await self._refresh_once()

    async def _refresh_once(self) -> None:
        self._service._projection_is_stale.clear()
        try:
            await self._service.refresh_the_view()
        except Exception:
            self._service._log.exception("projection refresh failed")

    async def _either(self, waiting: Coroutine[Any, Any, object]) -> None:
        """Wait for `waiting`, or for the stop signal, whichever comes first."""
        stopping = asyncio.create_task(self._stopping.wait())
        wanted = asyncio.create_task(waiting)
        try:
            await asyncio.wait({stopping, wanted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stopping, wanted):
                task.cancel()
