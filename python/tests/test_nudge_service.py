"""The nudge service's shell.

`test_nudges.py` covers which suggestions are true. These cover the three things
around that: the payload the browser reads, the insert that enforces "once,
ever", and which of them are worth a phone buzzing.

The payload test is the one with teeth. Writing `application_ids` instead of
`applicationIds` produces no error anywhere — the tick succeeds, the card
renders, and its button silently does nothing.
"""

from datetime import UTC, datetime, timedelta

import pytest

from loop.db import Database, Queue, claim
from loop.domain.nudges import Suggestion
from loop.services import NudgeService, suggestion_payload

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


class TestThePayload:
    """Pure — no database. The wire format the Today card is built from."""

    def test_it_is_camel_case_because_the_client_reads_it_directly(self) -> None:
        payload = suggestion_payload(_a_suggestion())

        assert set(payload) == {
            "key",
            "rule",
            "applicationIds",
            "kind",
            "meta",
            "title",
            "body",
            "cta",
            "expiresAt",
            "urgencyAt",
            "depth",
            "pushable",
            "bypassesBudget",
        }
        assert payload["applicationIds"] == ["0193f2"]

    def test_timestamps_are_written_the_way_every_other_one_is(self) -> None:
        payload = suggestion_payload(_a_suggestion())
        assert payload["urgencyAt"] == "2026-08-17T17:00:00.000Z"
        assert payload["expiresAt"] == "2026-08-17T17:00:00.000Z"

    def test_a_suggestion_that_never_expires_says_so_with_a_null(self) -> None:
        assert suggestion_payload(_a_suggestion(expires_at=None))["expiresAt"] is None


class TestTheTick:
    async def test_a_deadline_becomes_one_row_and_one_notification(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)
        await _a_deadline(db, user_id, application_id, due_in_days=4)

        ticked = await NudgeService(db).tick_user(user_id, now=NOW)

        assert (ticked.inserted, ticked.notified) == (1, 1)
        [message] = await claim(db, Queue.NOTIFY, batch=10)
        assert message.body["rule"] == "deadline"
        # The one rule allowed past the cap and the quiet window.
        assert message.body["bypasses_budget"] is True
        assert message.body["url"] == "/suggestions/deadline%3A" + application_id

    async def test_ticking_twice_does_not_buzz_twice(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)
        await _a_deadline(db, user_id, application_id, due_in_days=4)
        service = NudgeService(db)

        await service.tick_user(user_id, now=NOW)
        second = await service.tick_user(user_id, now=NOW)

        assert (second.inserted, second.notified) == (0, 0)
        assert len(await claim(db, Queue.NOTIFY, batch=10)) == 1

    async def test_the_payload_reaches_the_column_intact(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)
        await _a_deadline(db, user_id, application_id, due_in_days=4)

        await NudgeService(db).tick_user(user_id, now=NOW)

        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                "select application_ids, payload, expires_at from suggestions "
                "where user_id = $1",
                user_id,
            )
        # The column is uuid[] and the payload's copy is an array of strings;
        # `/api/today` spreads the payload, so the second is what the browser sees.
        assert [str(a) for a in row["application_ids"]] == [application_id]
        assert row["payload"]["applicationIds"] == [application_id]
        assert row["expires_at"] is not None


class TestWhatIsWorthAPhoneBuzzing:
    async def test_letting_something_go_is_never_a_push(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)
        await _gone_quiet_long_ago(db, user_id, application_id)

        ticked = await NudgeService(db).tick_user(user_id, now=NOW)

        assert ticked.inserted == 1
        assert ticked.notified == 0
        assert not await claim(db, Queue.NOTIFY, batch=10)

    async def test_a_dismissed_card_never_comes_back(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)
        await _a_deadline(db, user_id, application_id, due_in_days=4)
        service = NudgeService(db)
        await service.tick_user(user_id, now=NOW)

        async with db.session(user_id) as connection:
            await connection.execute(
                "update suggestions set dismissed_at = now() where user_id = $1", user_id
            )

        # The rule fires again — the key has left `open_or_issued` — and the
        # insert conflicts with the dismissed row, which is what "one per
        # application per rule, ever" actually means.
        again = await service.tick_user(user_id, now=NOW)

        assert (again.evaluated, again.inserted) == (1, 0)


def _a_suggestion(**over: object) -> Suggestion:
    base: dict[str, object] = {
        "key": "deadline:0193f2",
        "rule": "deadline",
        "application_ids": ("0193f2",),
        "kind": "deadline",
        "meta": "in 4 days",
        "title": "Prima take-home due Monday",
        "body": "Parsed from the gmail email.",
        "cta": "Open brief",
        "expires_at": datetime(2026, 8, 17, 17, 0, tzinfo=UTC),
        "urgency_at": datetime(2026, 8, 17, 17, 0, tzinfo=UTC),
        "depth": 6,
        "pushable": True,
        "bypasses_budget": True,
    }
    base.update(over)
    return Suggestion(**base)  # type: ignore[arg-type]


async def _an_application(db: Database, user_id: str) -> str:
    async with db.session(user_id) as connection:
        company = await connection.fetchval(
            """
            insert into companies (canonical_name) values ('Prima')
            on conflict (lower(canonical_name), coalesce(domain, '')) do update
              set canonical_name = excluded.canonical_name
            returning id
            """
        )
        return str(
            await connection.fetchval(
                """
                insert into applications
                  (user_id, company_id, role_title, current_stage, current_phase,
                   status, confidence, last_signal_at)
                values ($1,$2,'Engineer','take_home','interviewing','live',1.0,now())
                returning id
                """,
                user_id,
                company,
            )
        )


async def _a_deadline(
    db: Database, user_id: str, application_id: str, *, due_in_days: int
) -> None:
    async with db.session(user_id) as connection:
        await connection.execute(
            """
            insert into deadlines (user_id, application_id, kind, due_at, source)
            values ($1,$2,'take_home',$3,'gmail')
            """,
            user_id,
            application_id,
            NOW + timedelta(days=due_in_days),
        )


async def _gone_quiet_long_ago(db: Database, user_id: str, application_id: str) -> None:
    async with db.session(user_id) as connection:
        await connection.execute(
            """
            update applications
               set status = 'dormant', went_dormant_at = $2, last_signal_at = $2
             where id = $1
            """,
            application_id,
            NOW - timedelta(days=120),
        )
