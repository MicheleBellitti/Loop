"""The five budgets, and the two settings the port had got wrong.

`@fastify/rate-limit` was registered `global: false`, so only a route naming a
budget got one — and the port had dropped all five. Four of them are public, and
`/api/auth/recover` is the only working way into the product before a passkey is
enrolled, which makes an unthrottled password field the whole of the front door.
"""

import time

import pytest
from httpx import AsyncClient

from loop.api.ratelimit import SlidingWindow, TooManyRequests

pytestmark = pytest.mark.integration


class TestTheWindow:
    def test_lets_the_budget_through_and_no_more(self) -> None:
        window = SlidingWindow(limit=3, window_seconds=60, clock=lambda: 1000.0)
        for _ in range(3):
            window.check("ip:1.2.3.4")
        with pytest.raises(TooManyRequests):
            window.check("ip:1.2.3.4")

    def test_counts_each_caller_separately(self) -> None:
        window = SlidingWindow(limit=1, window_seconds=60, clock=lambda: 1000.0)
        window.check("ip:1.2.3.4")
        window.check("ip:5.6.7.8")  # not the same caller, not the same budget

    def test_slides_rather_than_resetting_on_a_boundary(self) -> None:
        # A fixed window lets twice the budget through across its edge: five
        # attempts at 14:59 and five more at 15:00 is ten in a minute against a
        # limit of five. On the recovery route that is the difference that
        # matters.
        now = [0.0]
        window = SlidingWindow(limit=2, window_seconds=10, clock=lambda: now[0])
        window.check("k")
        now[0] = 9.0
        window.check("k")
        now[0] = 9.5
        with pytest.raises(TooManyRequests):
            window.check("k")
        # The first hit ages out at 10, the second at 19.
        now[0] = 10.5
        window.check("k")
        now[0] = 11.0
        with pytest.raises(TooManyRequests):
            window.check("k")

    def test_says_how_long_to_wait(self) -> None:
        now = [0.0]
        window = SlidingWindow(limit=1, window_seconds=900, clock=lambda: now[0])
        window.check("k")
        now[0] = 300.0
        with pytest.raises(TooManyRequests) as raised:
            window.check("k")
        assert raised.value.retry_after == 600

    def test_forgets_a_caller_it_has_not_heard_from(self) -> None:
        # Otherwise the dictionary grows one entry per distinct address for the
        # life of the process, which on a public route is the cheapest denial of
        # service there is.
        now = [0.0]
        window = SlidingWindow(limit=5, window_seconds=10, clock=lambda: now[0])
        for i in range(2000):
            window.check(f"ip:{i}")
        now[0] = 100.0
        window.check("ip:fresh")
        assert len(window._hits) < 2000

    def test_uses_a_monotonic_clock(self) -> None:
        # A wall clock stepping backwards over an NTP correction or a DST change
        # would hand out a free window.
        assert SlidingWindow(limit=1, window_seconds=1).clock is time.monotonic


class TestTheRoutesThatHaveOne:
    async def test_the_recovery_password_is_five_attempts_per_quarter_hour(
        self, anonymous: AsyncClient, user_id: str
    ) -> None:
        wrong = {"password": "not-the-recovery-password"}
        codes = [
            (await anonymous.post("/api/auth/recover", json=wrong)).status_code
            for _ in range(6)
        ]
        # Five wrong passwords, then the door stops answering. scrypt already
        # makes each attempt expensive; this is the half scrypt cannot do.
        assert codes[:5] == [401] * 5
        assert codes[5] == 429

    async def test_and_says_when_to_come_back(
        self, anonymous: AsyncClient, user_id: str
    ) -> None:
        wrong = {"password": "not-the-recovery-password"}
        for _ in range(6):
            response = await anonymous.post("/api/auth/recover", json=wrong)
        assert response.status_code == 429
        assert response.json() == {
            "error": {
                "code": "rate_limited",
                "message": "Too many requests. Try again shortly.",
            }
        }
        assert 0 < int(response.headers["retry-after"]) <= 900

    async def test_signing_in_with_a_passkey_is_twenty_a_minute(
        self, anonymous: AsyncClient
    ) -> None:
        codes = [
            (await anonymous.post("/api/auth/login/options")).status_code for _ in range(21)
        ]
        assert codes[20] == 429
        assert 429 not in codes[:20]

    async def test_the_board_is_not_limited_at_all(self, client: AsyncClient) -> None:
        # There is no global budget, which is the reference's shape: everything
        # else is behind a session cookie and a derived CSRF token, and
        # rate-limiting a single tenant's own board reads costs more than it
        # buys.
        for _ in range(40):
            assert (await client.get("/api/applications")).status_code == 200
