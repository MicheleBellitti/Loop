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
from collections.abc import Awaitable, Callable
from typing import Any, Final

import asyncpg

from loop.db import Database
from loop.domain.thresholds import POLL_INTERVAL_SECONDS, WATCH_RENEW_EVERY_HOURS

from .connector import ConnectorService, mailbox_by_id

_log = logging.getLogger("loop.runtime")

PUSH_CHANNEL: Final = "loop_connector"
BACKFILL_CHANNEL: Final = "loop_backfill"


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


async def _awaited(service: Callable[[], Awaitable[None]]) -> None:
    await service()


async def until_signalled(*services: Callable[[], Awaitable[None]]) -> None:
    """Run everything until SIGTERM or SIGINT, then let it finish.

    Compose sends SIGTERM and waits ten seconds. That is long enough for a
    handler mid-write, and this is what makes the difference between a clean
    stop and a message redelivered into a half-applied state.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(name, stopping.set)

    running: list[asyncio.Task[None]] = [
        asyncio.create_task(_awaited(service)) for service in services
    ]
    waiting = asyncio.create_task(stopping.wait())
    await asyncio.wait([*running, waiting], return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
