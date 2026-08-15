"""Five budgets, on the five routes that have one.

There is no global limit, and that is the reference's shape rather than an
omission: `@fastify/rate-limit` was registered with `global: false`, so only a
route naming a budget got one. Everything else is behind a session cookie and a
derived CSRF token, and rate-limiting an authenticated single-tenant app's own
board reads would cost more than it buys.

The five that do have one are the five an unauthenticated caller can reach, or
that cost something real per call:

    POST /api/auth/recover        5 per 15 minutes   public, and the only way in
    POST /api/auth/login/options  20 per minute      public
    POST /api/auth/login/verify   20 per minute      public
    POST /api/gmail/push          600 per minute     public, Google's webhook
    POST /api/applications        60 per minute      fetches a posting URL

`/api/auth/recover` is the one that matters. It is public, it takes a password,
and a recovery password is the only working way into this product before a
passkey is enrolled. scrypt already makes each attempt expensive; this makes the
*number* of attempts finite, which is the half scrypt cannot do.

A sliding window over an in-process deque. Redis would be a second thing to
operate for a single-tenant app that receives forty messages a week, and a limit
that resets when the process restarts is the correct trade at this size — the
attacker who can restart your gateway has already won.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Request

from .errors import ApiError

# The five budgets, named so a route reads as the sentence above.
RECOVER = (5, 15 * 60)
LOGIN = (20, 60)
PUSH = (600, 60)
QUICK_ADD = (60, 60)


class TooManyRequests(ApiError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            status=429,
            code="rate_limited",
            message="Too many requests. Try again shortly.",
        )
        self.retry_after = retry_after

    def headers(self) -> dict[str, str]:
        return {"retry-after": str(self.retry_after)}


@dataclass
class SlidingWindow:
    """Hits per key, oldest dropped as they age out of the window.

    A sliding window rather than a fixed one because a fixed window lets twice
    the budget through across a boundary — ten attempts at 14:59 and ten more at
    15:00 is twenty in a minute against a limit of ten, which on the recovery
    route is the difference that matters.
    """

    limit: int
    window_seconds: float
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def check(self, key: str) -> None:
        """Records this hit, or raises with how long to wait."""
        now = self.clock()
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            raise TooManyRequests(retry_after=max(1, int(hits[0] + self.window_seconds - now)))

        hits.append(now)
        self._prune(cutoff)

    def _prune(self, cutoff: float) -> None:
        """Keys nobody has hit in a whole window are dropped.

        Without this the dictionary grows one entry per distinct client address
        for the life of the process, which on a public route is an attacker's
        cheapest denial of service.
        """
        if len(self._hits) < 1024:
            return
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]


def caller(request: Request) -> str:
    """The session if there is one, else the address.

    A session id is the better key: it survives a changing address and it cannot
    be spoofed. The four public routes have no session by definition, so those
    fall back to the peer address — which behind Caddy is Caddy, and is why the
    proxy sets `X-Forwarded-For` and why this reads it.
    """
    session = getattr(request.state, "session", None)
    if session is not None:
        return f"user:{session.user_id}"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # The left-most entry is the original client; everything after it was
        # added by a hop. Only trustworthy because nothing but our own proxy can
        # reach the port this listens on.
        return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def limit(budget: tuple[int, float]) -> Callable[[Request], None]:
    """One window per route per application.

    Per route, because `LOGIN` is one budget shared by two routes and the
    reference gave each its own — twenty attempts at `options` must not spend
    the twenty at `verify`. Per application, because the window is state, and
    state that outlives the app it belongs to is state two tests share.
    """
    here = object()

    def guard(request: Request) -> None:
        windows: dict[object, SlidingWindow] = request.app.state.limits
        window = windows.get(here)
        if window is None:
            window = windows[here] = SlidingWindow(
                limit=budget[0], window_seconds=budget[1]
            )
        window.check(caller(request))

    return guard
