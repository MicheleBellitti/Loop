"""The calendar half of the connector.

A calendar entry from a company already in the pipeline says an interview was
scheduled with more confidence than any sentence in any email, and it says
when. That makes two things load-bearing, and both are asserted here: that only
the entries touching a known company are read at all, and that the sync token
never advances over an entry that was not published — Google will not send it
again, so a token moved too early loses the interview for good.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import connect_mailbox

from loop.db import Database, Queue, claim
from loop.domain.wire import decode_raw_message
from loop.google.mailbox import Mailbox
from loop.services import ConnectorService

pytestmark = pytest.mark.integration


class FakeGoogle:
    """Only the calendar surface, which is all `_sync_calendar` touches."""

    def __init__(self, *pages: dict[str, Any]) -> None:
        self._pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    async def list_calendar_events(
        self,
        token: str,
        *,
        sync_token: str | None = None,
        time_min: str | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"sync_token": sync_token, "page_token": page_token})
        return self._pages[len(self.calls) - 1]


class Exploding(FakeGoogle):
    async def list_calendar_events(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("calendar is down")


def an_event(
    *,
    event_id: str = "evt-1",
    organiser: str = "recruiter@prima.it",
    summary: str = "Technical interview",
    status: str = "confirmed",
    starts_at: datetime | None = None,
) -> dict[str, Any]:
    moment = starts_at or datetime.now(UTC) + timedelta(days=3)
    return {
        "id": event_id,
        "status": status,
        "summary": summary,
        "organizer": {"email": organiser},
        "attendees": [{"email": "someone@pytest.invalid"}],
        "start": {"dateTime": moment.isoformat()},
        "end": {"dateTime": (moment + timedelta(hours=1)).isoformat()},
    }


async def a_mailbox(db: Database, user_id: str) -> Mailbox:
    mailbox_id = await connect_mailbox(db, user_id)
    return Mailbox(
        id=mailbox_id,
        user_id=user_id,
        provider="gmail",
        address="someone@pytest.invalid",
        secret_ciphertext=b"\x00",
        secret_nonce=b"\x00",
        dek_wrapped=b"\x00",
        dek_nonce=b"\x00",
        scopes=(),
        cursor={},
        watch_expires_at=None,
        status="ok",
        last_ok_at=None,
    )


async def an_application_at(db: Database, user_id: str, domain: str) -> None:
    """A company with a domain, which is what makes its invitations relevant."""
    async with db.session(user_id) as connection:
        # `companies` is not user-scoped, so it outlives the user fixture and a
        # second test finds the row already there.
        company = await connection.fetchval(
            """
            insert into companies (canonical_name, domain) values ($1, $2)
            on conflict (lower(canonical_name), coalesce(domain, '')) do nothing
            returning id
            """,
            domain.split(".")[0],
            domain,
        ) or await connection.fetchval(
            "select id from companies where domain = $1", domain
        )
        await connection.execute(
            """
            insert into applications
              (user_id, company_id, role_title, current_stage, current_phase,
               manually_created, confidence)
            values ($1,$2,'Backend engineer','applied','sent',true,1.0)
            """,
            user_id,
            company,
        )


class TestWhatItReads:
    async def test_an_invitation_from_a_known_company_reaches_the_queue(
        self, db: Database, user_id: str
    ) -> None:
        await an_application_at(db, user_id, "prima.it")
        mailbox = await a_mailbox(db, user_id)
        google = FakeGoogle({"items": [an_event()], "nextSyncToken": "tok-1"})

        await ConnectorService(db, google)._sync_calendar(mailbox, "access")  # type: ignore[arg-type]

        message = await claim(db, Queue.RAW, batch=10)
        assert len(message) == 1
        raw = decode_raw_message(message[0].body)
        assert raw.provider_message_id == "cal:evt-1"
        assert raw.invite is not None
        assert raw.invite.summary == "Technical interview"
        assert raw.invite.status == "confirmed"

    async def test_a_stranger_in_the_calendar_is_none_of_its_business(
        self, db: Database, user_id: str
    ) -> None:
        await an_application_at(db, user_id, "prima.it")
        mailbox = await a_mailbox(db, user_id)
        google = FakeGoogle(
            {"items": [an_event(organiser="dentist@example.com")], "nextSyncToken": "t"}
        )

        await ConnectorService(db, google)._sync_calendar(mailbox, "access")  # type: ignore[arg-type]

        assert await claim(db, Queue.RAW, batch=10) == []

    async def test_a_cancellation_gets_through_after_the_invitation(
        self, db: Database, user_id: str
    ) -> None:
        # "Cancellations matter as much as creations": an interview called off
        # is a fact about the application, and it arrives as a second event
        # with the same id.
        await an_application_at(db, user_id, "prima.it")
        mailbox = await a_mailbox(db, user_id)
        google = FakeGoogle(
            {"items": [an_event()], "nextSyncToken": "t1"},
            {"items": [an_event(status="cancelled")], "nextSyncToken": "t2"},
        )
        service = ConnectorService(db, google)  # type: ignore[arg-type]

        await service._sync_calendar(mailbox, "access")
        await service._sync_calendar(mailbox, "access")

        published = await claim(db, Queue.RAW, batch=10)
        invites = [decode_raw_message(m.body).invite for m in published]
        assert [i.status for i in invites if i] == ["confirmed", "cancelled"]

    async def test_the_same_invitation_twice_is_published_once(
        self, db: Database, user_id: str
    ) -> None:
        await an_application_at(db, user_id, "prima.it")
        mailbox = await a_mailbox(db, user_id)
        google = FakeGoogle(
            {"items": [an_event()], "nextSyncToken": "t1"},
            {"items": [an_event()], "nextSyncToken": "t2"},
        )
        service = ConnectorService(db, google)  # type: ignore[arg-type]

        await service._sync_calendar(mailbox, "access")
        await service._sync_calendar(mailbox, "access")

        assert len(await claim(db, Queue.RAW, batch=10)) == 1


class TestTheSyncToken:
    async def test_it_advances_only_after_the_events_are_published(
        self, db: Database, user_id: str
    ) -> None:
        await an_application_at(db, user_id, "prima.it")
        mailbox = await a_mailbox(db, user_id)
        google = FakeGoogle({"items": [an_event()], "nextSyncToken": "tok-1"})

        await ConnectorService(db, google)._sync_calendar(mailbox, "access")  # type: ignore[arg-type]

        async with db.session(user_id) as connection:
            cursor = await connection.fetchval(
                "select cursor from mailbox_accounts where id = $1", mailbox.id
            )
        assert cursor["syncToken"] == "tok-1"
        assert len(await claim(db, Queue.RAW, batch=10)) == 1

    async def test_a_calendar_that_will_not_answer_leaves_the_cursor_alone(
        self, db: Database, user_id: str
    ) -> None:
        # The old token has to stand: moving it on a failed read would skip
        # every event the failed read did not return.
        mailbox = await a_mailbox(db, user_id)
        async with db.session(user_id) as connection:
            await connection.execute(
                """update mailbox_accounts set cursor = '{"syncToken": "old"}'::jsonb
                    where id = $1""",
                mailbox.id,
            )

        await ConnectorService(db, Exploding())._sync_calendar(mailbox, "access")  # type: ignore[arg-type]

        async with db.session(user_id) as connection:
            cursor = await connection.fetchval(
                "select cursor from mailbox_accounts where id = $1", mailbox.id
            )
        assert cursor["syncToken"] == "old"
