"""The open connection, so the board moves without being asked to.

Postgres does the fan-out. The pipeline and the connector call `pg_notify` on
`loop_events` after they have committed, one process here holds a `listen`, and
every browser attached to that process gets the frame. No polling, no Redis, and
the notification is emitted after the transaction so a client never learns about
a row it cannot yet read.

Two things about this response are unlike every other one. It carries no
security headers, because it is a stream rather than a document and the
middleware never sees it complete. And it carries `x-accel-buffering: no`,
without which a proxy holds the frames until it has enough of them to be worth
forwarding — which is to say, forever.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Final

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from loop.api import auth

router = APIRouter(prefix="/api")

_log = logging.getLogger("loop.api.stream")

CHANNEL: Final = "loop_events"

# Proxies drop an idle connection at thirty seconds. This is a comment frame
# rather than an event: it keeps the socket warm without the client having to
# know about a heartbeat it should ignore.
HEARTBEAT_SECONDS: Final = 25

_HEADERS = {
    "cache-control": "no-cache, no-transform",
    "connection": "keep-alive",
    "x-accel-buffering": "no",
}


class Broadcaster:
    """One `listen` for the process, one queue per attached browser.

    Held on the app rather than at module scope so two apps in one test run —
    which is exactly what the suite does — do not share a listener.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._connection: asyncpg.Connection | None = None
        self._listeners: dict[int, tuple[str, asyncio.Queue[str]]] = {}
        self._next_id = 0

    async def start(self) -> None:
        self._connection = await asyncpg.connect(self._dsn)
        await self._connection.add_listener(CHANNEL, self._on_notify)

    async def stop(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def attach(self, user_id: str) -> tuple[int, asyncio.Queue[str]]:
        self._next_id += 1
        # Bounded: a browser that has stopped reading must not grow a queue
        # until the process runs out of memory. Dropping frames is survivable
        # because every one of them only says "something changed" — the client
        # refetches and gets the truth.
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._listeners[self._next_id] = (user_id, queue)
        return self._next_id, queue

    def detach(self, listener_id: int) -> None:
        self._listeners.pop(listener_id, None)

    def _on_notify(self, *args: Any) -> None:
        payload = args[-1]
        try:
            event = json.loads(payload or "{}")
        except ValueError:
            # A malformed notification is not worth taking the gateway down for.
            _log.warning("unparseable notification on %s", CHANNEL)
            return
        kind, user_id = event.get("type"), event.get("user_id")
        if not kind or not user_id:
            return
        frame = f"event: {kind}\ndata: {json.dumps(event)}\n\n"
        for listener_user, queue in self._listeners.values():
            if listener_user != user_id:
                continue
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    session = auth.require(getattr(request.state, "session", None))
    broadcaster: Broadcaster = request.app.state.broadcaster
    listener_id, queue = broadcaster.attach(session.user_id)

    async def frames() -> AsyncIterator[str]:
        # A comment, not an event: it tells the client the socket is open
        # without giving it anything to handle.
        yield ": connected\n\n"
        try:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            broadcaster.detach(listener_id)

    return StreamingResponse(frames(), media_type="text/event-stream", headers=_HEADERS)
