"""How a service stops, and what happens when one does not stop but falls over.

Both halves of this were unenforced and both were wrong in the same direction —
towards a process that looks fine. A SIGTERM cancelled the handler mid-write
that every docstring in `services/` promises to let finish, and a crash was
gathered into silence and reported to Compose as a clean exit, which for a
container told `restart: unless-stopped` means it is never coming back.

Pure: no database, no signals. `until_signalled` is driven through the same
`Service` protocol the real six satisfy, and the shutdown is triggered by the
services themselves returning rather than by a signal, which is the one part of
the path a test cannot deliver to itself without racing the runner.
"""

import asyncio

import pytest

from loop.services.runtime import until_signalled


class Recorder:
    """A service that runs until asked, and remembers how it was ended."""

    def __init__(self, *, finish_after: float = 0.0) -> None:
        self.stopped = False
        self.cancelled = False
        self.finished = False
        self._stopping = asyncio.Event()
        self._finish_after = finish_after

    async def run(self) -> None:
        try:
            await self._stopping.wait()
            # Stands in for the message in hand: a handler that is part-way
            # through a write when the signal arrives.
            await asyncio.sleep(self._finish_after)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True

    async def stop(self) -> None:
        self.stopped = True
        self._stopping.set()


class Crasher:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def run(self) -> None:
        raise self._error

    async def stop(self) -> None:
        return None


class Bystander(Recorder):
    """Ends only because something else did."""


class Ender:
    """Returns straight away, which is what stands in for the signal here.

    `until_signalled` waits for the first of {a service ending, SIGTERM}, and a
    test that delivered itself a real signal would be racing the runner and
    every other test sharing the loop.
    """

    async def run(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class TestStopping:
    async def test_a_service_is_asked_before_it_is_cancelled(self) -> None:
        service = Recorder(finish_after=0.01)
        # One service that ends immediately is what stands in for the signal;
        # the other has to be brought down by the shutdown path itself.
        await until_signalled(Ender(), service, grace=2.0)
        assert service.stopped
        assert service.finished
        assert not service.cancelled

    async def test_one_that_will_not_finish_is_cancelled_at_the_grace(self) -> None:
        stubborn = Recorder(finish_after=30.0)
        async with asyncio.timeout(5):
            await until_signalled(Ender(), stubborn, grace=0.05)
        assert stubborn.stopped
        assert stubborn.cancelled
        assert not stubborn.finished

    async def test_the_others_are_brought_down_when_one_ends(self) -> None:
        bystander = Bystander()
        await until_signalled(Ender(), bystander, grace=2.0)
        assert bystander.stopped


class TestCrashing:
    async def test_a_service_that_raises_does_not_look_like_a_clean_stop(self) -> None:
        with pytest.raises(RuntimeError, match="the mailbox went away"):
            await until_signalled(Crasher(RuntimeError("the mailbox went away")), grace=1.0)

    async def test_the_others_are_still_stopped_first(self) -> None:
        bystander = Bystander()
        with pytest.raises(RuntimeError):
            await until_signalled(Crasher(RuntimeError("boom")), bystander, grace=2.0)
        assert bystander.stopped
        assert not bystander.cancelled

    async def test_nothing_to_run_is_not_an_error(self) -> None:
        await until_signalled()
