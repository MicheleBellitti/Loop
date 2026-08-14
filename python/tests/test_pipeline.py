"""The single writer, and the loop that feeds it."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from loop.db import Database, Message, Queue, claim, depth, publish
from loop.domain.messages import EventSource, PendingEvent
from loop.domain.wire import encode_pending_event
from loop.services import Consumer, ConsumerOptions, PipelineService

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


async def _application(db: Database, user_id: str) -> str:
    async with db.session(user_id) as connection:
        company = await connection.fetchval(
            """
            insert into companies (canonical_name, domain) values ($1, $2)
            on conflict (lower(canonical_name), coalesce(domain, '')) do update
              set canonical_name = excluded.canonical_name
            returning id
            """,
            "Prima",
            "prima.it",
        )
        return str(
            await connection.fetchval(
                """
                insert into applications
                  (user_id, company_id, role_title, current_stage, current_phase, confidence)
                values ($1,$2,'Machine Learning Engineer','applied','sent',0.9) returning id
                """,
                user_id,
                company,
            )
        )


def _pending(user_id: str, application_id: str, **over: object) -> PendingEvent:
    base: dict[str, object] = {
        "user_id": user_id,
        "application_id": application_id,
        "type": "acknowledged",
        "occurred_at": NOW,
        "confidence": 0.95,
        "to_stage": "acknowledged",
        "evidence_ref": "msg-1",
        "rung": 1,
    }
    base.update(over)
    return PendingEvent(**base)  # type: ignore[arg-type]


class TestApplyingOneEvent:
    async def test_appends_projects_and_notifies(self, db: Database, user_id: str) -> None:
        application_id = await _application(db, user_id)
        result = await PipelineService(db).apply(_pending(user_id, application_id))

        assert result.event_id is not None
        assert not result.duplicate
        assert result.notified

        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                "select current_stage, current_phase from applications where id = $1",
                application_id,
            )
        assert (row["current_stage"], row["current_phase"]) == ("acknowledged", "sent")

    async def test_a_redelivery_appends_nothing_and_buzzes_nothing(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _application(db, user_id)
        pipeline = PipelineService(db)
        first = await pipeline.apply(_pending(user_id, application_id))
        second = await pipeline.apply(_pending(user_id, application_id))

        assert first.notified
        assert second.duplicate
        assert not second.notified

        async with db.session(user_id) as connection:
            assert (
                await connection.fetchval(
                    "select count(*) from application_events where application_id = $1",
                    application_id,
                )
                == 1
            )

    async def test_but_it_still_projects_so_a_crash_between_the_two_heals(
        self, db: Database, user_id: str
    ) -> None:
        # The reference returned early on a duplicate. A process that died
        # between the append and the projection therefore left the row stale
        # for good: the redelivery found the event already there and skipped
        # the very step that had been missed.
        application_id = await _application(db, user_id)
        pipeline = PipelineService(db)
        await pipeline.apply(_pending(user_id, application_id))

        async with db.session(user_id) as connection:
            await connection.execute(
                "update applications set current_stage = 'applied' where id = $1",
                application_id,
            )
        await pipeline.apply(_pending(user_id, application_id))

        async with db.session(user_id) as connection:
            stage = await connection.fetchval(
                "select current_stage from applications where id = $1", application_id
            )
        assert stage == "acknowledged"

    async def test_exactly_one_first_touch_however_many_claim_it(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _application(db, user_id)
        pipeline = PipelineService(db)
        for index, channel in enumerate(("linkedin", "career_page")):
            await pipeline.apply(
                _pending(
                    user_id,
                    application_id,
                    type="applied",
                    to_stage="applied",
                    evidence_ref=f"msg-{index}",
                    occurred_at=NOW + timedelta(minutes=index),
                    source=EventSource(channel=channel, is_first_touch=True),
                )
            )
        async with db.session(user_id) as connection:
            first_touches = await connection.fetchval(
                "select count(*) from sources where application_id = $1 and is_first_touch",
                application_id,
            )
            channels = await connection.fetchval(
                "select count(*) from sources where application_id = $1", application_id
            )
        assert (first_touches, channels) == (1, 2)

    async def test_a_replay_is_silent(self, db: Database, user_id: str) -> None:
        application_id = await _application(db, user_id)
        result = await PipelineService(db).apply(_pending(user_id, application_id, silent=True))
        assert not result.notified


class TestTheConsumerLoop:
    async def test_takes_a_message_off_the_queue_and_acknowledges_it(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _application(db, user_id)
        async with db.untenanted() as connection:
            await connection.execute("delete from mq.messages where queue = $1", Queue.EVENT)
            await publish(
                connection,
                Queue.EVENT,
                encode_pending_event(_pending(user_id, application_id)),
            )

        await _drain(PipelineService(db).consumer(Queue.EVENT))

        async with db.untenanted() as connection:
            assert await depth(connection, Queue.EVENT) == 0
        async with db.session(user_id) as connection:
            assert (
                await connection.fetchval(
                    "select count(*) from application_events where application_id = $1",
                    application_id,
                )
                == 1
            )

    async def test_a_handler_that_fails_leaves_the_message_for_another_try(
        self, db: Database
    ) -> None:
        async with db.untenanted() as connection:
            await connection.execute("delete from mq.messages where queue = $1", Queue.NOTIFY)
            await publish(connection, Queue.NOTIFY, {"n": 1})

        async def always_fails(_message: object) -> None:
            raise RuntimeError("nope")

        await _drain(
            Consumer(
                db,
                Queue.NOTIFY,
                always_fails,  # type: ignore[arg-type]
                options=ConsumerOptions(batch=1, visibility=5),
            )
        )

        # Still queued, still hidden until its lease runs out, and the attempt
        # counted. A failure must not look like an acknowledgement.
        async with db.untenanted() as connection:
            row = await connection.fetchrow(
                "select read_ct, vt > now() as hidden from mq.messages where queue = $1",
                Queue.NOTIFY,
            )
            await connection.execute("delete from mq.messages where queue = $1", Queue.NOTIFY)
        assert row["read_ct"] == 1
        assert row["hidden"] is True

    async def test_a_message_stays_hidden_while_it_is_being_worked(
        self, db: Database
    ) -> None:
        """The lease is granted once per batch and spent one message at a time.

        Two messages at 0.8 s each under a one-second lease: the second one's
        original lease expires at t=1.0, while its handler runs from 0.8 to 1.6.
        In that window a second reader — the other container, the same service
        after a restart — can claim a message that is still in flight, work it
        twice, and climb its attempt count on deliveries no handler ever saw.

        So the assertion is what a second reader sees at t=1.2, and it is
        nothing.
        """
        async with db.untenanted() as connection:
            await connection.execute("delete from mq.messages where queue = $1", Queue.NOTIFY)
            for n in range(2):
                await publish(connection, Queue.NOTIFY, {"n": n})

        async def slow(_message: Message) -> None:
            await asyncio.sleep(0.8)

        consumer = Consumer(
            db, Queue.NOTIFY, slow, options=ConsumerOptions(batch=2, visibility=1)
        )
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(1.2)
        stolen = await claim(db, Queue.NOTIFY, batch=2, visibility=30)

        await consumer.stop()
        task.cancel()
        assert stolen == []

    async def test_stopping_waits_for_the_message_in_hand(self, db: Database) -> None:
        async with db.untenanted() as connection:
            await connection.execute("delete from mq.messages where queue = $1", Queue.NOTIFY)
            await publish(connection, Queue.NOTIFY, {"n": 1})

        # Waited for rather than slept towards. A fixed sleep here is a bet that
        # the loop has claimed a batch, extended its lease and entered the
        # handler within it — two round trips to Postgres — and on a loaded CI
        # runner it loses: `stop()` then finds the consumer still idle, returns
        # at once, and the assertion fails without anything being wrong.
        started = asyncio.Event()
        finished = asyncio.Event()

        async def slow(_message: object) -> None:
            started.set()
            await asyncio.sleep(0.1)
            finished.set()

        consumer = Consumer(
            db,
            Queue.NOTIFY,
            slow,  # type: ignore[arg-type]
            options=ConsumerOptions(batch=1),
        )
        task = asyncio.create_task(consumer.run())
        async with asyncio.timeout(10):
            await started.wait()
        await consumer.stop()
        # Killing a handler mid-write is how a message ends up half applied.
        assert finished.is_set()
        await task

    async def test_a_message_that_keeps_failing_is_parked_with_its_body_off(
        self, db: Database
    ) -> None:
        async with db.untenanted() as connection:
            for queue in (Queue.NOTIFY, f"{Queue.NOTIFY}_dlq"):
                await connection.execute("delete from mq.messages where queue = $1", queue)
            await publish(connection, Queue.NOTIFY, {"user_id": "u", "text": "private"})

        async def always_fails(_message: object) -> None:
            raise RuntimeError("nope")

        # Visibility zero, so each failure makes it immediately visible again
        # and one drain is enough to burn every attempt it is allowed.
        await _drain(
            Consumer(
                db,
                Queue.NOTIFY,
                always_fails,  # type: ignore[arg-type]
                options=ConsumerOptions(batch=1, visibility=0, idle_poll=0.01),
            )
        )

        async with db.untenanted() as connection:
            assert await depth(connection, Queue.NOTIFY) == 0
            parked = await connection.fetchval(
                "select message from mq.messages where queue = $1", f"{Queue.NOTIFY}_dlq"
            )
            await connection.execute(
                "delete from mq.messages where queue = $1", f"{Queue.NOTIFY}_dlq"
            )
        assert parked["user_id"] == "u"
        assert parked["text"] == "[stripped]"


async def _drain(consumer: Consumer, seconds: float = 0.3) -> None:
    """Run the loop for a while, then stop it the way a deployment would.

    `stop` waits for the message in hand, so the window only has to be long
    enough to start the last handler — except where the point of the test is
    that the handlers are slow, and there the number is the test.
    """
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(seconds)
    await consumer.stop()
    await task


class TestNotification:
    async def test_a_listener_hears_about_the_change(
        self, db: Database, dsn: str, user_id: str
    ) -> None:
        import asyncpg

        application_id = await _application(db, user_id)
        heard: list[dict[str, str]] = []
        listener = await asyncpg.connect(dsn)
        try:
            await listener.add_listener(
                "loop_events", lambda _c, _p, _ch, payload: heard.append(json.loads(payload))
            )
            await PipelineService(db).apply(_pending(user_id, application_id))
            await asyncio.sleep(0.2)
        finally:
            await listener.close()

        assert heard and heard[0]["application_id"] == application_id
