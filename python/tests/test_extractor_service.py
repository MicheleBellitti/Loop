"""The ladder's shell.

`test_ladder.py` covers what the rungs read. What these add is the four things
the service does with the answer, and one thing it refuses to do with a message
that has no answer left in it.
"""

from typing import Any

import pytest
from conftest import MAILBOX_ADDRESS, record_message

from loop.db import Database, Message, Queue, claim
from loop.domain.messages import CandidateMessage
from loop.domain.wire import decode_candidate_message
from loop.ladder import Ladder, LadderContext
from loop.ladder.contracts import Extraction
from loop.services import ExtractorService, TransientRungError

pytestmark = pytest.mark.integration


def candidate(body: dict[str, Any]) -> CandidateMessage:
    return decode_candidate_message({**body, "score": 5, "cheap_only": False})


class TestTheSuccessPath:
    async def test_a_reading_becomes_a_signal_and_leaves_the_verdict_open(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        body = await record_message(
            db,
            user_id,
            mailbox_id,
            sender="Greenhouse <no-reply@greenhouse.io>",
            subject="Your application to Prima",
            text="Thank you for applying. We have received your application.",
        )

        reading = await ExtractorService(db).extract(candidate(body))

        assert reading.outcome == "extracted"
        [message] = await claim(db, Queue.SIGNAL, batch=10)
        assert message.body["intent"] == reading.intent
        assert message.body["excerpt"]
        # The resolver owns the terminal verdict. Closing the row here would
        # call a message finished before anything had been placed.
        assert await _outcome(db, user_id, mailbox_id, body) == (None, False)


class TestTheThreadMap:
    async def test_a_reply_inherits_the_application_its_thread_belongs_to(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)
        await _an_event_on_thread(db, user_id, application_id, "thread-1")
        body = await record_message(
            db,
            user_id,
            mailbox_id,
            sender="Greenhouse <no-reply@greenhouse.io>",
            subject="Your application to Prima",
            text="Thank you for applying. We have received your application.",
            thread_id="thread-1",
        )

        await ExtractorService(db).extract(candidate(body))

        [message] = await claim(db, Queue.SIGNAL, batch=10)
        assert message.body["application_hint"] == application_id

    async def test_a_thread_seen_under_two_applications_resolves_to_the_later(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        # The reference read this with an unordered `distinct` and got whichever
        # row Postgres returned last, which is how a merge made the answer
        # depend on the plan.
        older = await _an_application(db, user_id)
        newer = await _an_application(db, user_id)
        await _an_event_on_thread(db, user_id, older, "thread-2", days_ago=9)
        await _an_event_on_thread(db, user_id, newer, "thread-2", days_ago=1)

        context = await ExtractorService(db)._context_for(user_id)

        assert context.thread_to_application["thread-2"] == newer


class TestTheTerminalPaths:
    async def test_your_own_reply_is_recorded_and_no_one_is_asked_about_it(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        body = await record_message(
            db,
            user_id,
            mailbox_id,
            sender=f"Me <{MAILBOX_ADDRESS}>",
            subject="Re: colloquio",
            text="Confermo volentieri l'invito per giovedì.",
        )

        reading = await ExtractorService(db).extract(candidate(body))

        assert reading.outcome == "ignored"
        assert await _outcome(db, user_id, mailbox_id, body) == ("dropped", True)
        assert await _open_reviews(db, user_id) == 0

    async def test_an_unreadable_message_is_asked_about_once(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        body = await record_message(
            db,
            user_id,
            mailbox_id,
            sender="Someone <someone@example.org>",
            subject="Re: la posizione",
            text="Ti scrivo in merito a quanto ci siamo detti.",
        )
        service = ExtractorService(db)

        assert (await service.extract(candidate(body))).outcome == "review"
        await service.extract(candidate(body))

        # The reference wrote `on conflict do nothing` on a table whose only
        # unique constraint is a generated primary key, so a redelivery raised
        # the same question a second time.
        assert await _open_reviews(db, user_id) == 1
        async with db.session(user_id) as connection:
            excerpt = await connection.fetchval(
                "select excerpt from review_items where user_id = $1", user_id
            )
        assert excerpt
        assert await _outcome(db, user_id, mailbox_id, body) == ("review", True)

    async def test_a_rung_that_cannot_be_reached_parks_rather_than_guesses(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        body = await record_message(db, user_id, mailbox_id)

        reading = await ExtractorService(db, ladder=Ladder([_UnreachableRung()])).extract(
            candidate(body)
        )

        assert reading.outcome == "parked"
        assert await _outcome(db, user_id, mailbox_id, body) == ("parked", True)
        assert not await claim(db, Queue.SIGNAL, batch=10)


class TestTheDrainStub:
    async def test_the_four_key_stub_is_absorbed_rather_than_dead_lettered(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        # `drain_parked` enqueues a message with no body, because the body was
        # deleted with the queue row. Raising on it would dead-letter the stub
        # and leave the row invisible to both the drain and the escalation.
        body = await record_message(db, user_id, mailbox_id)
        async with db.session(user_id) as connection:
            await connection.execute(
                """
                update seen_messages set outcome = null
                 where mailbox_id = $1 and provider_message_id = $2
                """,
                mailbox_id,
                body["provider_message_id"],
            )

        stub = Message(
            msg_id=1,
            body={
                "user_id": user_id,
                "mailbox_id": mailbox_id,
                "provider_message_id": body["provider_message_id"],
                "replay": True,
            },
            read_count=1,
        )
        await ExtractorService(db).handle(stub)

        # Back to parked, and `processed_at` still untouched — the drain's own
        # attempt counter is what ends this, not the extractor.
        assert await _outcome(db, user_id, mailbox_id, body) == ("parked", False)


class _UnreachableRung:
    """A model rung that is not answering."""

    @property
    def costly(self) -> bool:
        return True

    def extract(self, msg: CandidateMessage, ctx: LadderContext) -> Extraction | None:
        raise TransientRungError("unreachable")


async def _outcome(
    db: Database, user_id: str, mailbox_id: str, body: dict[str, Any]
) -> tuple[str | None, bool]:
    async with db.session(user_id) as connection:
        row = await connection.fetchrow(
            """
            select outcome, processed_at from seen_messages
             where mailbox_id = $1 and provider_message_id = $2
            """,
            mailbox_id,
            body["provider_message_id"],
        )
    return row["outcome"], row["processed_at"] is not None


async def _open_reviews(db: Database, user_id: str) -> int:
    async with db.session(user_id) as connection:
        return int(
            await connection.fetchval(
                "select count(*) from review_items where user_id = $1 and resolved_at is null",
                user_id,
            )
        )


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
                  (user_id, company_id, role_title, current_stage, current_phase, confidence)
                values ($1,$2,'Engineer','applied','sent',1.0)
                returning id
                """,
                user_id,
                company,
            )
        )


async def _an_event_on_thread(
    db: Database, user_id: str, application_id: str, thread_id: str, *, days_ago: int = 1
) -> None:
    """Stands in for the pipeline, which is the only writer of this table."""
    async with db.session(user_id) as connection:
        await connection.execute(
            """
            insert into application_events
              (application_id, user_id, type, occurred_at, payload, confidence, rung)
            values ($1,$2,'acknowledged', now() - make_interval(days => $3), $4, 0.9, 1)
            """,
            application_id,
            user_id,
            days_ago,
            {"thread_id": thread_id},
        )
