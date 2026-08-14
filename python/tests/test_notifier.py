"""Every rule in the notifier is a reason not to send.

So these tests are mostly about silence: what does not arrive, and why. The
sender is substituted throughout — not to avoid the network, but because "the
push was refused" is the assertion, and it is only an assertion if sending is
something that could have happened.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from loop.db import Database, Queue, claim
from loop.domain.messages import PendingNotification
from loop.services import NotifierService, PushPayload, PushResult, PushSubscription

pytestmark = pytest.mark.integration

ROME = ZoneInfo("Europe/Rome")

# A Thursday at nine, well outside the quiet window.
MORNING = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


class Recorder:
    """A sender that remembers, and answers however the test needs it to."""

    def __init__(self, answers: dict[str, PushResult] | None = None) -> None:
        self.sent: list[tuple[PushSubscription, PushPayload]] = []
        self._answers = answers or {}

    async def send(
        self, subscription: PushSubscription, payload: PushPayload
    ) -> PushResult:
        self.sent.append((subscription, payload))
        return self._answers.get(subscription.endpoint, "ok")


def notification(user_id: str, **over: object) -> PendingNotification:
    base: dict[str, object] = {
        "user_id": user_id,
        "suggestion_key": "prepare:0193f2",
        "rule": "prepare",
        "title": "Prima technical interview tomorrow",
        "body": "The posting, the thread, and everything you wrote about them.",
        "url": "/suggestions/prepare%3A0193f2",
    }
    base.update(over)
    return PendingNotification(**base)  # type: ignore[arg-type]


class TestWhoMayInterruptYou:
    async def test_a_rule_on_the_never_pushed_list_never_reaches_a_device(
        self, db: Database, user_id: str
    ) -> None:
        sender = Recorder()

        result = await NotifierService(db, sender=sender).deliver(
            notification(user_id, rule="let_it_go")
        )

        assert result.outcome == "suppressed"
        assert not sender.sent

    async def test_the_second_notification_of_a_day_is_dropped_not_deferred(
        self, db: Database, user_id: str, subscription: str
    ) -> None:
        sender = Recorder()
        service = NotifierService(db, sender=sender)
        morning = MORNING

        assert (await service.deliver(notification(user_id), now=morning)).outcome == "sent"
        second = await service.deliver(
            notification(user_id, suggestion_key="follow_up_due:0193f3"), now=morning
        )

        assert second.outcome == "suppressed"
        assert second.reason == "daily cap"
        assert len(sender.sent) == 1
        # Dropped rather than requeued: tomorrow's slot belongs to whatever is
        # true tomorrow.
        assert not await claim(db, Queue.NOTIFY, batch=10)

    async def test_a_deadline_passes_the_cap_and_still_spends_it(
        self, db: Database, user_id: str, subscription: str
    ) -> None:
        sender = Recorder()
        service = NotifierService(db, sender=sender)
        morning = MORNING

        await service.deliver(
            notification(user_id, rule="deadline", bypasses_budget=True), now=morning
        )
        after = await service.deliver(
            notification(user_id, suggestion_key="prepare:0193f9"), now=morning
        )

        # The exemption is one-way. A deadline ignores the budget and still
        # writes the row that is the budget.
        assert after.outcome == "suppressed"
        assert len(sender.sent) == 1


class TestQuietHours:
    async def test_a_late_notification_waits_for_the_morning(
        self, db: Database, user_id: str, subscription: str
    ) -> None:
        sender = Recorder()
        late = datetime(2026, 8, 13, 22, 30, tzinfo=ROME).astimezone(UTC)

        result = await NotifierService(db, sender=sender).deliver(
            notification(user_id), now=late
        )

        assert result.outcome == "deferred"
        assert not sender.sent
        # On the queue but not visible: the delay is what makes it arrive in the
        # morning, so claiming now must find nothing.
        assert not await claim(db, Queue.NOTIFY, batch=10)
        async with db.untenanted() as connection:
            row = await connection.fetchrow(
                """
                select message, extract(epoch from vt - now()) as delay
                  from mq.messages where queue = $1
                """,
                Queue.NOTIFY,
            )
        assert row["message"]["suggestion_key"] == "prepare:0193f2"
        # `mq.send` measures the delay from the server's clock, so a pinned
        # `now` fixes how long the wait is rather than when it ends. From 22:30
        # to 08:00 is nine and a half hours.
        assert round(float(row["delay"]) / 3600, 1) == 9.5

    async def test_a_deadline_is_the_one_thing_allowed_to_wake_you(
        self, db: Database, user_id: str, subscription: str
    ) -> None:
        sender = Recorder()
        late = datetime(2026, 8, 13, 22, 30, tzinfo=ROME).astimezone(UTC)

        result = await NotifierService(db, sender=sender).deliver(
            notification(user_id, rule="deadline", bypasses_budget=True), now=late
        )

        assert result.outcome == "sent"
        assert len(sender.sent) == 1

    async def test_the_day_boundary_is_the_users_own(
        self, db: Database, user_id: str, subscription: str
    ) -> None:
        sender = Recorder()
        service = NotifierService(db, sender=sender)
        # 23:30 UTC on the 13th is 01:30 on the 14th in Rome — a different day,
        # and therefore a different budget. Both are inside the quiet window, so
        # both are deadlines to get past it.
        first = MORNING
        after_midnight_in_rome = datetime(2026, 8, 13, 23, 30, tzinfo=UTC)

        await service.deliver(
            notification(user_id, rule="deadline", bypasses_budget=True), now=first
        )
        async with db.session(user_id) as connection:
            dates = await connection.fetch(
                "select local_date from notifications_sent where user_id = $1", user_id
            )
        assert [row["local_date"].isoformat() for row in dates] == ["2026-08-13"]

        await service.deliver(
            notification(
                user_id,
                rule="deadline",
                bypasses_budget=True,
                suggestion_key="deadline:0193ff",
            ),
            now=after_midnight_in_rome,
        )
        async with db.session(user_id) as connection:
            dates = await connection.fetch(
                "select local_date from notifications_sent where user_id = $1 order by 1",
                user_id,
            )
        assert [row["local_date"].isoformat() for row in dates] == [
            "2026-08-13",
            "2026-08-14",
        ]


class TestTheDevices:
    """All pinned to a weekday morning.

    Without a fixed instant these read the wall clock, and after 21:00 local the
    quiet-hours gate defers everything — so they passed all day and failed in
    the evening, which is the worst kind of test.
    """

    async def test_nothing_to_send_to_is_not_a_failure(
        self, db: Database, user_id: str
    ) -> None:
        result = await NotifierService(db, sender=Recorder()).deliver(
            notification(user_id), now=MORNING
        )
        assert result.outcome == "no_subscription"

    async def test_a_subscription_the_browser_threw_away_is_removed(
        self, db: Database, user_id: str, subscription: str
    ) -> None:
        dead = await _subscribe(db, user_id, "https://push.example/dead")
        sender = Recorder({dead: "gone"})

        result = await NotifierService(db, sender=sender).deliver(
            notification(user_id), now=MORNING
        )

        assert result.outcome == "sent"
        assert result.count == 1
        async with db.session(user_id) as connection:
            remaining = await connection.fetch(
                "select endpoint from push_subscriptions where user_id = $1", user_id
            )
        assert [row["endpoint"] for row in remaining] == [subscription]

    async def test_when_every_device_refuses_nothing_is_recorded(
        self, db: Database, user_id: str, subscription: str
    ) -> None:
        sender = Recorder({subscription: "failed"})

        result = await NotifierService(db, sender=sender).deliver(
            notification(user_id), now=MORNING
        )

        assert result.outcome == "failed"
        async with db.session(user_id) as connection:
            assert not await connection.fetchval(
                "select count(*) from notifications_sent where user_id = $1", user_id
            )


class TestTheServiceWorkerContract:
    async def test_four_keys_and_a_tag_that_collapses_a_redelivery(
        self, db: Database, user_id: str, subscription: str
    ) -> None:
        sender = Recorder()

        await NotifierService(db, sender=sender).deliver(
            notification(user_id), now=MORNING
        )

        [(_sub, payload)] = sender.sent
        assert set(payload.as_json()) == {"title", "body", "url", "tag"}
        # Delivery is at-least-once and the push happens before the row that
        # records it, so the tag is the only thing standing between a redelivery
        # and a second buzz.
        assert payload.tag == "prepare:0193f2"


@pytest.fixture
async def subscription(db: Database, user_id: str) -> str:
    return await _subscribe(db, user_id, "https://push.example/first")


async def _subscribe(db: Database, user_id: str, endpoint: str) -> str:
    async with db.session(user_id) as connection:
        await connection.execute(
            """
            insert into push_subscriptions (user_id, endpoint, p256dh, auth)
            values ($1,$2,'a-public-key','an-auth-secret')
            """,
            user_id,
            endpoint,
        )
    return endpoint
