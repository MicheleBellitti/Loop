"""Starting the long-running halves, and stopping them without losing work.

Four of the six services are queue consumers and need nothing but a loop. The
connector is the exception: it is woken by Postgres notifications as well as by
a clock, because a push from Gmail should not wait five minutes for a poll and a
poll must still happen when the push subscription has lapsed.

Shutdown waits for the message in hand. Killing a handler mid-write is how a
message ends up half applied and then redelivered into a state nobody designed
for, and a few seconds of patience at SIGTERM is the whole of the fix.
"""

import asyncio
import contextlib
import json
import logging
import signal
from typing import Any, Final, Protocol

import asyncpg

from loop.db import Database
from loop.domain.thresholds import POLL_INTERVAL_SECONDS, WATCH_RENEW_EVERY_HOURS

from .connector import ConnectorService, mailbox_by_id

_log = logging.getLogger("loop.runtime")

PUSH_CHANNEL: Final = "loop_connector"
BACKFILL_CHANNEL: Final = "loop_backfill"

# Compose sends SIGTERM and waits ten seconds before SIGKILL. This is the window
# inside it that a handler mid-write gets to finish in.
SHUTDOWN_GRACE_SECONDS: Final = 8.0


class Service(Protocol):
    """Something that runs until it is asked to stop.

    Both halves are the contract. `until_signalled` used to take a bare
    `Callable[[], Awaitable[None]]` — the bound `.run` — which gave it no way to
    reach the `stop()` every service in this package implements, so SIGTERM
    cancelled the task instead of asking it to finish. That is precisely the
    "killing a handler mid-write" the docstrings on `Consumer.stop` and on this
    module both promise not to do.
    """

    async def run(self) -> None: ...

    async def stop(self) -> None: ...


class ConnectorRuntime:
    """The connector's clock and its doorbell.

    Three things wake it: a push notification relayed through Postgres, a
    backfill the user asked for, and a poll that runs whether or not either of
    those is working. The poll is what makes a lapsed watch a slowdown instead
    of a silence.
    """

    def __init__(self, db: Database, connector: ConnectorService) -> None:
        self._db = db
        self._connector = connector
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._backfills: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        listener = await asyncpg.connect(self._db.dsn)
        try:
            await listener.add_listener(PUSH_CHANNEL, self._on_push)
            await listener.add_listener(BACKFILL_CHANNEL, self._on_backfill)
            await self._renew_watches()
            renewals = asyncio.create_task(self._renew_periodically())
            try:
                await self._loop()
            finally:
                renewals.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewals
        finally:
            await listener.close()
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            await self._drain_backfills()
            try:
                await self._connector.sync_all()
            except Exception:
                # One bad pass must not end the process: the next poll is five
                # minutes away and the mailbox is still there.
                _log.exception("sync failed")
            await self._sleep_or_wake()

    async def _drain_backfills(self) -> None:
        while not self._backfills.empty():
            mailbox_id, months = self._backfills.get_nowait()
            async with self._db.untenanted() as connection:
                mailbox = await mailbox_by_id(connection, mailbox_id)
            if mailbox is None:
                _log.warning("backfill asked for an unknown mailbox %s", mailbox_id)
                continue
            try:
                await self._connector.backfill(mailbox, months)
            except Exception:
                _log.exception("backfill failed for %s", mailbox_id)

    async def _sleep_or_wake(self) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=POLL_INTERVAL_SECONDS)

    async def _renew_periodically(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=WATCH_RENEW_EVERY_HOURS * 3600
                )
                return
            await self._renew_watches()

    async def _renew_watches(self) -> None:
        """Daily, against a seven-day expiry. Room for six failures in a row."""
        for mailbox in await self._connector._mailboxes():
            await self._connector.renew_watch(mailbox)

    def _on_push(self, *_args: Any) -> None:
        self._wake.set()

    def _on_backfill(self, *args: Any) -> None:
        try:
            asked = json.loads(args[-1] or "{}")
            self._backfills.put_nowait((str(asked["mailbox_id"]), int(asked["months"])))
        except (ValueError, KeyError):
            _log.warning("unreadable backfill request")
            return
        self._wake.set()


async def until_signalled(
    *services: Service, grace: float = SHUTDOWN_GRACE_SECONDS
) -> None:
    """Run everything until SIGTERM or SIGINT, then let it finish.

    Ask, wait, then insist. `stop()` is what lets a handler complete the write it
    is in the middle of; the grace window bounds how long that may take, and only
    what is still running when it closes is cancelled.

    A service that raises is re-raised here rather than gathered into silence.
    Every one of these runs as its own container with `restart: unless-stopped`,
    and a crashed process that exits 0 is one Compose has been told to leave
    alone — the mailbox stops being read and nothing anywhere says so.
    """
    if not services:
        return

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(name, stopping.set)

    running = {asyncio.create_task(service.run()): service for service in services}
    waiting = asyncio.create_task(stopping.wait())
    await asyncio.wait([*running, waiting], return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting

    # Concurrently with the run tasks winding down, not before them: a `stop()`
    # that waits for the message in hand — which is what `Consumer.stop` does —
    # can only return once the loop it is stopping has made that progress.
    stops = [asyncio.create_task(service.stop()) for service in running.values()]
    deadline = loop.time() + grace
    _, pending = await asyncio.wait(running, timeout=grace)
    for task in pending:
        _log.warning("a service did not stop within %.0fs; cancelling it", grace)
        task.cancel()
    outcomes = await asyncio.gather(*running, return_exceptions=True)

    # And `stop()` gets what is left of the same window, rather than being
    # cancelled the instant `run()` returns. The work a `stop()` has after its
    # run loop ends is exactly the work that could not be done while it was
    # still going: the pipeline's final `refresh materialized view` is one, and
    # it always lost that race, because setting the stop flag is what ends
    # `run()` and issuing the refresh is the statement after it. Losing it left
    # every phase-reach ratio on the funnel reading pre-shutdown data.
    if stops:
        _, unfinished = await asyncio.wait(stops, timeout=max(0.0, deadline - loop.time()))
        for task in unfinished:
            _log.warning("a service did not finish stopping within %.0fs; cancelling it", grace)
            task.cancel()
        await asyncio.gather(*stops, return_exceptions=True)

    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(
            outcome, asyncio.CancelledError
        ):
            raise outcome
