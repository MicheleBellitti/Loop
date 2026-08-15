"""The cheap filter's shell.

`test_classifier.py` covers what the score is. What these add is everything the
service does around it: reading the context out of the database, passing the
body on without amputating it, and writing down the drops — which is the only
record from which a false negative is ever recovered.
"""

import pytest
from conftest import record_message

from loop.db import Database, Queue, claim
from loop.services import ClassifierService

pytestmark = pytest.mark.integration


class TestScreening:
    async def test_an_ats_acknowledgement_passes_and_carries_its_verdict(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        body = await record_message(
            db,
            user_id,
            mailbox_id,
            sender="Greenhouse <no-reply@greenhouse.io>",
            subject="Your application to Prima",
            text="Thank you for your application. We have received it.",
        )

        verdict = await ClassifierService(db).screen(body)

        assert verdict.outcome == "pass"
        assert verdict.published
        [message] = await claim(db, Queue.CANDIDATE, batch=10)
        assert message.body["score"] == verdict.score
        assert message.body["cheap_only"] is False
        assert message.body["reasons"]

    async def test_the_body_arrives_whole(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        # The verdict is added to the payload, never re-encoded from it: a rule
        # file may match on any header at all, and the codec writes eight.
        body = await record_message(db, user_id, mailbox_id)
        body["headers"]["in_reply_to"] = "<parent@mail.gmail.com>"

        await ClassifierService(db).screen(body)

        [message] = await claim(db, Queue.CANDIDATE, batch=10)
        assert message.body["headers"]["in_reply_to"] == "<parent@mail.gmail.com>"

    async def test_a_drop_is_recorded_rather_than_forgotten(
        self, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        body = await record_message(
            db,
            user_id,
            mailbox_id,
            sender="Zalando <news@newsletter.zalando.it>",
            subject="La selezione in saldo, fino al 50%",
            text="Approfitta delle offerte di questa settimana.",
        )

        verdict = await ClassifierService(db).screen(body)

        assert verdict.outcome == "drop"
        assert not verdict.published
        assert not await claim(db, Queue.CANDIDATE, batch=10)
        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                """
                select outcome, processed_at from seen_messages
                 where mailbox_id = $1 and provider_message_id = $2
                """,
                mailbox_id,
                body["provider_message_id"],
            )
        assert row["outcome"] == "dropped"
        assert row["processed_at"] is not None


class TestTheContext:
    async def test_a_company_added_after_the_load_waits_for_the_ttl(
        self, db: Database, user_id: str
    ) -> None:
        service = ClassifierService(db)
        assert await service.context_for(user_id) is not None

        await _an_application_at(db, user_id, "prima.it")

        assert "prima.it" not in (await service.context_for(user_id)).company_domains
        expired = ClassifierService(db, ttl=0)
        assert "prima.it" in (await expired.context_for(user_id)).company_domains

    async def test_a_user_with_nothing_yet_is_not_an_error(
        self, db: Database, user_id: str
    ) -> None:
        context = await ClassifierService(db).context_for(user_id)
        assert context.company_domains == frozenset()
        assert context.known_threads == frozenset()
        # The vendor list still arrives, which is what does the work on a
        # mailbox connected five minutes ago.
        assert context.ats_domains


async def _an_application_at(db: Database, user_id: str, domain: str) -> None:
    async with db.session(user_id) as connection:
        company = await connection.fetchval(
            """
            insert into companies (canonical_name, domain) values ($1, $2)
            on conflict (lower(canonical_name), coalesce(domain, '')) do update
              set canonical_name = excluded.canonical_name
            returning id
            """,
            domain.split(".")[0].title(),
            domain,
        )
        await connection.execute(
            """
            insert into applications
              (user_id, company_id, role_title, current_stage, current_phase, confidence)
            values ($1,$2,'Engineer','applied','sent',1.0)
            """,
            user_id,
            company,
        )
