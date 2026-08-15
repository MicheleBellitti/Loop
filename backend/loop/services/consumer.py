"""The loop every queue-driven service runs.

One class, four services, no handler logic. It exists to make one shape
unavoidable:

    claim a batch          → one short transaction, already committed
    work the message       → no transaction, no connection held
    write and acknowledge  → one short transaction

The middle step is where a model call goes. Holding a pooled connection across
it is the defect this whole port is organised around not repeating, and a
runtime that never hands the handler a connection is a stronger guarantee than a
rule everyone has to remember.

A handler that raises leaves its message alone. It becomes visible again when
the lease expires and is tried again; after enough failures it is dead-lettered
with its body stripped rather than retried forever or dropped.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loop.db import Database, Message, acknowledge, claim, dead_letter
from loop.db.queue import extend_lease
from loop.domain.thresholds import (
    BACKOFF_MAX_SECONDS,
    BACKOFF_MIN_SECONDS,
    VISIBILITY_TIMEOUT_SECONDS,
)

Handler = Callable[[Message], Awaitable[None]]

# How long to wait before asking an empty queue again. Short enough that a
# message the connector just pushed is picked up promptly, long enough that an
# idle mailbox is not a busy loop.
IDLE_POLL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ConsumerOptions:
    # Small by default. A batch is claimed under one lease and worked serially,
    # so a large one only pays off when the handler is fast — and the lease is
    # refreshed per message either way.
    batch: int = 5
    visibility: int = VISIBILITY_TIMEOUT_SECONDS
    idle_poll: float = IDLE_POLL_SECONDS


class Consumer:
    def __init__(
        self,
        db: Database,
        queue: str,
        handler: Handler,
        *,
        options: ConsumerOptions | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._db = db
        self._queue = queue
        self._handler = handler
        self._options = options or ConsumerOptions()
        self._log = log or logging.getLogger(f"loop.{queue}")
        self._stopping = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()

    async def run(self) -> None:
        """Poll until stopped. Errors back off; they never spin."""
        backoff = BACKOFF_MIN_SECONDS
        while not self._stopping.is_set():
            try:
                messages = await claim(
                    self._db,
                    self._queue,
                    batch=self._options.batch,
                    visibility=self._options.visibility,
                )
            except Exception:
                # The database is unreachable or the queue is gone. Retrying at
                # a thousand a second helps nobody and hides the cause in the
                # log; back off and let the health check notice.
                self._log.exception("could not claim from %s", self._queue)
                await self._wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                continue

            backoff = BACKOFF_MIN_SECONDS
            if not messages:
                await self._wait(self._options.idle_poll)
                continue

            for message in messages:
                if self._stopping.is_set():
                    # The rest of the batch stays claimed until its lease runs
                    # out, then comes back. Nothing is lost by leaving now.
                    break
                await self._work(message)

    async def stop(self) -> None:
        """Stop claiming, and wait for the message in hand to finish.

        Killing a handler mid-write is how a message ends up half applied and
        then redelivered into a state nobody designed for.
        """
        self._stopping.set()
        await self._idle.wait()

    async def _work(self, message: Message) -> None:
        self._idle.clear()
        try:
            # The lease was granted to the whole batch at claim time; this
            # message's turn may be a while after that.
            await extend_lease(
                self._db, self._queue, message.msg_id, seconds=self._options.visibility
            )
            await self._handler(message)
        except Exception:
            self._log.exception(
                "handler failed for %s msg %s (attempt %s)",
                self._queue,
                message.msg_id,
                message.read_count,
            )
            if message.exhausted:
                await self._park(message)
            return
        else:
            await acknowledge(self._db, self._queue, message.msg_id)
        finally:
            self._idle.set()

    async def _park(self, message: Message) -> None:
        try:
            await dead_letter(self._db, self._queue, message)
            self._log.error(
                "dead-lettered %s msg %s after %s attempts",
                self._queue,
                message.msg_id,
                message.read_count,
            )
        except Exception:
            # Failing to park it leaves it in the queue, where it will be tried
            # again and parked again. That is the better failure.
            self._log.exception("could not dead-letter %s msg %s", self._queue, message.msg_id)

    async def _wait(self, seconds: float) -> None:
        """Sleep, unless someone asks us to stop first."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            return
