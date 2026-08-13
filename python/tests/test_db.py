"""What only a real database can answer.

Marked `integration` and skipped without a `DATABASE_URL`, so the pure suite
still runs in a fraction of a second with nothing installed.
"""

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from loop.db import (
    Database,
    Message,
    Queue,
    acknowledge,
    append_event,
    apply_side_effects,
    claim,
    dead_letter,
    depth,
    load_events,
    project_application,
    publish,
)
from loop.domain.messages import PendingEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


async def _application(db: Database, user_id: str, role: str = "Backend Engineer") -> str:
    async with db.session(user_id) as connection:
        company = await connection.fetchval(
            """
            insert into companies (canonical_name, domain) values ($1, $2)
            on conflict (lower(canonical_name), coalesce(domain, '')) do update
              set canonical_name = excluded.canonical_name
            returning id
            """,
            "Nexi",
            "nexi.it",
        )
        return str(
            await connection.fetchval(
                """
                insert into applications
                  (user_id, company_id, role_title, current_stage, current_phase, confidence)
                values ($1,$2,$3,'applied','sent',0.9) returning id
                """,
                user_id,
                company,
                role,
            )
        )


def _event(user_id: str, application_id: str, **over: object) -> PendingEvent:
    base: dict[str, object] = {
        "user_id": user_id,
        "application_id": application_id,
        "type": "acknowledged",
        "occurred_at": NOW,
        "confidence": 0.95,
        "evidence_ref": "msg-1",
        "rung": 1,
        "to_stage": "acknowledged",
    }
    base.update(over)
    return PendingEvent(**base)  # type: ignore[arg-type]


class TestTheMigrations:
    async def test_the_interview_stage_exists_and_the_rounds_moved_up(
        self, db: Database, user_id: str
    ) -> None:
        async with db.session(user_id) as connection:
            rows = await connection.fetch(
                "select key, phase, depth from stage_defs where user_id = $1 order by depth",
                user_id,
            )
        depths = {r["key"]: r["depth"] for r in rows}
        assert depths["interview"] == 7
        assert depths["technical"] == 8
        assert depths["final"] == 11
        # The interviewing band still starts where the screening band ends.
        assert depths["take_home"] < depths["interview"]
        by_key = {r["key"]: r["phase"] for r in rows}
        assert by_key["interview"] == "interviewing"

    async def test_running_them_again_changes_nothing(self, dsn: str) -> None:
        import asyncpg

        from loop.db import migrate

        connection = await asyncpg.connect(dsn)
        try:
            result = await migrate(connection)
        finally:
            await connection.close()
        assert result.applied == []
        assert "012_interview_stage.sql" in result.already_applied


class TestAppendingEvents:
    async def test_the_same_message_delivered_twice_appends_once(
        self, db: Database, user_id: str
    ) -> None:
        # A queue redelivery must not produce a second row, and must not buzz a
        # phone a second time — which is what the null return is for.
        application_id = await _application(db, user_id)
        async with db.session(user_id) as connection:
            first = await append_event(connection, _event(user_id, application_id))
            second = await append_event(connection, _event(user_id, application_id))
        assert first is not None
        assert second is None

    async def test_the_log_cannot_be_rewritten(self, db: Database, user_id: str) -> None:
        application_id = await _application(db, user_id)
        async with db.session(user_id) as connection:
            await append_event(connection, _event(user_id, application_id))
        with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="append-only"):
            async with db.session(user_id) as connection:
                await connection.execute(
                    "update application_events set confidence = 0.1 where user_id = $1", user_id
                )

    async def test_another_users_rows_are_not_visible(
        self, db: Database, dsn: str, user_id: str
    ) -> None:
        # Only as a service role. The owner is a superuser and a superuser
        # bypasses row-level security entirely — policies, FORCE and all — so a
        # port that connected as the owner would have no tenant isolation at
        # all, however carefully the policies are written.
        application_id = await _application(db, user_id)
        async with db.session(user_id) as connection:
            await append_event(connection, _event(user_id, application_id))

        async with db.untenanted() as connection:
            stranger = await connection.fetchval(
                "insert into users (email, tz) values ($1, 'UTC') returning id",
                f"stranger-{uuid.uuid4().hex}@pytest.invalid",
            )
        try:
            async with Database(dsn, role="loop_resolver") as scoped:
                async with scoped.session(str(stranger)) as connection:
                    assert await load_events(connection, application_id) == []
                # And the same role, asked about its own user, still sees.
                async with scoped.session(user_id) as connection:
                    assert len(await load_events(connection, application_id)) == 1
        finally:
            async with db.untenanted() as connection:
                await connection.execute("select erase_user($1)", stranger)

    async def test_only_the_pipeline_may_write_an_event(
        self, db: Database, dsn: str, user_id: str
    ) -> None:
        # The single-writer rule, held by grants rather than by everyone
        # remembering. The resolver decides which application a signal belongs
        # to and then hands it on; it cannot append.
        application_id = await _application(db, user_id)
        async with Database(dsn, role="loop_resolver") as scoped:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                async with scoped.session(user_id) as connection:
                    await append_event(connection, _event(user_id, application_id))


class TestTheProjection:
    async def test_rebuilds_the_row_from_the_log_alone(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _application(db, user_id)
        async with db.session(user_id) as connection:
            await append_event(
                connection,
                _event(user_id, application_id, type="applied", to_stage="applied"),
            )
            await append_event(
                connection,
                _event(
                    user_id,
                    application_id,
                    type="stage_advanced",
                    to_stage="hr_call",
                    occurred_at=NOW + timedelta(days=3),
                    evidence_ref="msg-2",
                ),
            )
            await project_application(connection, user_id, application_id)
            row = await connection.fetchrow(
                "select current_stage, current_phase, status, applied_at, awaiting_them"
                " from applications where id = $1",
                application_id,
            )
        assert row["current_stage"] == "hr_call"
        assert row["current_phase"] == "screening"
        assert row["status"] == "live"
        assert row["applied_at"] == NOW
        assert row["awaiting_them"] is True

    async def test_an_untitled_invitation_reaches_interviewing_without_naming_a_round(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _application(db, user_id)
        async with db.session(user_id) as connection:
            await append_event(
                connection,
                _event(
                    user_id,
                    application_id,
                    type="interview_scheduled",
                    to_stage="interview",
                    payload={"stage": "interview", "starts_at": NOW, "status": "confirmed"},
                ),
            )
            await project_application(connection, user_id, application_id)
            row = await connection.fetchrow(
                "select current_stage, current_phase from applications where id = $1",
                application_id,
            )
        assert row["current_phase"] == "interviewing"
        assert row["current_stage"] == "interview"


class TestInterviewSideEffects:
    async def test_a_scheduled_round_is_written_once_and_updated_in_place(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _application(db, user_id)
        async with db.session(user_id) as connection:
            for starts in (NOW, NOW + timedelta(hours=2)):
                await apply_side_effects(
                    connection,
                    user_id,
                    application_id,
                    _domain_event(
                        {
                            "stage": "system_design",
                            "starts_at": starts,
                            "calendar_event_id": "ev-1",
                            "status": "confirmed",
                        }
                    ),
                )
            rows = await connection.fetch(
                "select stage, starts_at, cancelled_at from interviews where user_id = $1",
                user_id,
            )
        assert len(rows) == 1
        assert rows[0]["starts_at"] == NOW + timedelta(hours=2)
        assert rows[0]["cancelled_at"] is None

    async def test_a_cancellation_actually_cancels(self, db: Database, user_id: str) -> None:
        # The reference set `cancelled_at = null` on every conflict and never
        # set it anywhere, so cancelling an interview reinstated it.
        application_id = await _application(db, user_id)
        async with db.session(user_id) as connection:
            await apply_side_effects(
                connection,
                user_id,
                application_id,
                _domain_event(
                    {
                        "stage": "technical",
                        "starts_at": NOW,
                        "calendar_event_id": "ev-1",
                        "status": "confirmed",
                    }
                ),
            )
            await apply_side_effects(
                connection,
                user_id,
                application_id,
                _domain_event(
                    {"starts_at": NOW, "calendar_event_id": "ev-1", "status": "cancelled"}
                ),
            )
            row = await connection.fetchrow(
                "select stage, cancelled_at from interviews where user_id = $1", user_id
            )
        assert row["cancelled_at"] is not None
        # And the round it was is still known: a cancellation says when, not which.
        assert row["stage"] == "technical"


class TestTheQueue:
    async def test_a_claim_hides_a_message_without_holding_a_transaction(
        self, db: Database
    ) -> None:
        async with db.untenanted() as connection:
            await publish(connection, Queue.RAW, {"hello": "world"})

        claimed = await claim(db, Queue.RAW, batch=10, visibility=30)
        assert [m.body for m in claimed] == [{"hello": "world"}]
        # Invisible to the next reader, and the first reader is holding nothing.
        assert await claim(db, Queue.RAW, batch=10, visibility=30) == []
        assert await acknowledge(db, Queue.RAW, claimed[0].msg_id) is True

    async def test_a_message_comes_back_when_its_visibility_expires(self, db: Database) -> None:
        async with db.untenanted() as connection:
            await publish(connection, Queue.SIGNAL, {"n": 1})
        first = await claim(db, Queue.SIGNAL, visibility=0)
        again = await claim(db, Queue.SIGNAL, visibility=30)
        assert [m.msg_id for m in first] == [m.msg_id for m in again]
        assert again[0].read_count == 2
        await acknowledge(db, Queue.SIGNAL, again[0].msg_id)

    async def test_a_dead_letter_keeps_the_evidence_and_drops_the_body(
        self, db: Database
    ) -> None:
        async with db.untenanted() as connection:
            await connection.execute(
                "delete from mq.messages where queue = any($1::text[])",
                [Queue.CANDIDATE, f"{Queue.CANDIDATE}_dlq"],
            )
            await publish(
                connection, Queue.CANDIDATE, {"provider_message_id": "m1", "text": "private"}
            )
        [message] = await claim(db, Queue.CANDIDATE, visibility=30)
        await dead_letter(db, Queue.CANDIDATE, message)

        async with db.untenanted() as connection:
            assert await depth(connection, Queue.CANDIDATE) == 0
            parked = await connection.fetchval(
                "select message from mq.messages where queue = $1", f"{Queue.CANDIDATE}_dlq"
            )
        assert parked["provider_message_id"] == "m1"
        assert parked["text"] == "[stripped]"

    def test_a_message_knows_when_it_has_had_enough_attempts(self) -> None:
        assert not Message(1, {}, read_count=5).exhausted
        assert Message(1, {}, read_count=6).exhausted


def _domain_event(payload: dict[str, object]):
    from loop.domain.types import DomainEvent

    return DomainEvent(
        type="interview_scheduled",
        occurred_at=NOW,
        confidence=0.97,
        evidence_ref="msg-1",
        rung=2,
        payload=payload,
    )
