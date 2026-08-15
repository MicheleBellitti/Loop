"""The resolver's shell, and the whole downstream half end to end.

The decisions are tested without a database in `test_resolver.py`. What these
add is everything the shell does around them: canonicalising a company against
rows that already exist, asking a human when it should, merging a duplicate
reversibly, and handing the result to the pipeline.
"""

from datetime import UTC, datetime, timedelta

import pytest

from loop.db import Database, Queue, claim, depth
from loop.domain.messages import Signal
from loop.domain.wire import decode_pending_event
from loop.services import PipelineService, ResolverService

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def signal(user_id: str, **over: object) -> Signal:
    base: dict[str, object] = {
        "user_id": user_id,
        "mailbox_id": "00000000-0000-0000-0000-000000000000",
        "provider_message_id": "msg-1",
        "evidence_ref": "msg-1",
        "intent": "acknowledged",
        "occurred_at": NOW,
        "confidence": 0.95,
        "rung": 1,
        "language": "en",
        "company": "Prima",
        "sender_domain": "prima.it",
        "role": "Machine Learning Engineer",
        "role_normalised": "machine learning engineer",
    }
    base.update(over)
    return Signal(**base)  # type: ignore[arg-type]


async def _drain_events(db: Database) -> None:
    """Let the pipeline catch up, the way it would in a running system."""
    pipeline = PipelineService(db)
    for message in await claim(db, Queue.EVENT, batch=50, visibility=30):
        await pipeline.handle(message)


async def _clear_queues(db: Database) -> None:
    async with db.untenanted() as connection:
        await connection.execute(
            "delete from mq.messages where queue = any($1::text[])",
            [Queue.EVENT, Queue.SIGNAL],
        )


class TestPlacingASignal:
    async def test_creates_the_application_and_enqueues_its_event(
        self, db: Database, user_id: str
    ) -> None:
        await _clear_queues(db)
        result = await ResolverService(db).resolve(signal(user_id))

        assert result.outcome == "placed"
        assert result.application_id is not None
        assert result.events == 1

        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                """
                select a.role_title, c.canonical_name, c.domain
                  from applications a join companies c on c.id = a.company_id
                 where a.id = $1
                """,
                result.application_id,
            )
        assert row["role_title"] == "Machine Learning Engineer"
        assert (row["canonical_name"], row["domain"]) == ("Prima", "prima.it")

        [message] = await claim(db, Queue.EVENT, visibility=5)
        pending = decode_pending_event(message.body)
        assert pending.type == "acknowledged"
        assert pending.application_id == result.application_id

    async def test_a_second_message_about_the_same_job_joins_it(
        self, db: Database, user_id: str
    ) -> None:
        await _clear_queues(db)
        resolver = ResolverService(db)
        first = await resolver.resolve(signal(user_id))
        second = await resolver.resolve(
            signal(
                user_id,
                provider_message_id="msg-2",
                evidence_ref="msg-2",
                intent="rejected",
                occurred_at=NOW + timedelta(days=3),
            )
        )
        assert second.application_id == first.application_id

        async with db.session(user_id) as connection:
            applications = await connection.fetchval(
                "select count(*) from applications where user_id = $1", user_id
            )
        assert applications == 1

    async def test_a_pair_the_user_pulled_apart_stays_apart(
        self, db: Database, user_id: str
    ) -> None:
        # `_candidates` reads the undo-merge back out of the event log, and
        # `find_duplicate` compares application ids against it. Aggregating the
        # wrong key made every candidate's `split_from` the literal string
        # 'split', so the guard could never fire and the resolver would merge
        # the same pair again the next time a similar signal arrived.
        await _clear_queues(db)
        resolver = ResolverService(db)
        kept = await resolver.resolve(signal(user_id))
        freed = "00000000-0000-0000-0000-00000000beef"

        async with db.session(user_id) as connection:
            company_id = await connection.fetchval(
                "select company_id from applications where id = $1", kept.application_id
            )
            await connection.execute(
                """
                insert into application_events
                  (user_id, application_id, type, occurred_at, confidence, rung, payload)
                values ($1,$2,'human_corrected',now(),1.0,4,$3)
                """,
                user_id,
                kept.application_id,
                # A dict, not `json.dumps`: the jsonb codec encodes it, and
                # doing it twice stores a JSON string that reads back as one.
                {"field": "merge", "from": "merged", "to": "split", "merged_id": freed},
            )
            candidates = await resolver._candidates(
                connection, str(company_id), signal(user_id)
            )

        [mine] = [c for c in candidates if c.id == kept.application_id]
        assert mine.split_from == frozenset({freed})

    async def test_one_employer_spelled_two_ways_is_one_company(
        self, db: Database, user_id: str
    ) -> None:
        # "ION Group" from an ATS display name and "iongroup" from the company's
        # own domain were two companies, two pipelines and two sets of numbers.
        await _clear_queues(db)
        resolver = ResolverService(db)
        first = await resolver.resolve(
            signal(user_id, company="ION Group", sender_domain=None, role="Backend Engineer")
        )
        second = await resolver.resolve(
            signal(
                user_id,
                provider_message_id="msg-2",
                evidence_ref="msg-2",
                company="iongroup",
                sender_domain=None,
                role="Backend Engineer",
            )
        )
        assert first.application_id == second.application_id

    async def test_a_thread_it_already_owns_needs_no_matching_at_all(
        self, db: Database, user_id: str
    ) -> None:
        await _clear_queues(db)
        resolver = ResolverService(db)
        first = await resolver.resolve(signal(user_id, thread_id="t1"))

        # The thread map is read from `application_events`, which the *pipeline*
        # writes — so the events have to land before the next signal can inherit
        # from them. That ordering is real and it is a race: two messages on one
        # thread resolved before the pipeline drains create two applications.
        # A live mailbox delivers them minutes apart; a backfill delivers them
        # together, which is where it bites.
        await _drain_events(db)

        # A different company and a different role, but the same thread. The
        # thread is the strongest and cheapest signal there is.
        second = await resolver.resolve(
            signal(
                user_id,
                provider_message_id="msg-2",
                evidence_ref="msg-2",
                thread_id="t1",
                company="Somewhere Else",
                sender_domain="elsewhere.test",
                role=None,
                role_normalised=None,
                intent="rejected",
            )
        )
        assert second.application_id == first.application_id

    async def test_an_intent_that_claims_nothing_places_nothing(
        self, db: Database, user_id: str
    ) -> None:
        await _clear_queues(db)
        result = await ResolverService(db).resolve(signal(user_id, intent="other"))
        assert result.outcome == "dropped"
        assert result.events == 0
        async with db.untenanted() as connection:
            assert await depth(connection, Queue.EVENT) == 0


class TestAskingAHuman:
    async def test_two_candidates_too_close_to_call_become_a_review_item(
        self, db: Database, user_id: str
    ) -> None:
        await _clear_queues(db)
        # Two genuinely different jobs at one company. Seeding the same role
        # twice would not set this up: they would be merged as duplicates, which
        # is the resolver working correctly.
        resolver = ResolverService(db)
        for index, role in enumerate(("Backend Engineer", "Data Scientist")):
            await resolver.resolve(
                signal(
                    user_id,
                    provider_message_id=f"seed-{index}",
                    evidence_ref=f"seed-{index}",
                    role=role,
                    role_normalised=role.lower(),
                )
            )

        # A roleless message at a company with two open applications: nothing
        # tells them apart, so the system asks rather than guessing.
        result = await resolver.resolve(
            signal(
                user_id,
                provider_message_id="msg-x",
                evidence_ref="msg-x",
                intent="rejected",
                role=None,
                role_normalised=None,
                excerpt="we will not be moving forward",
            )
        )
        assert result.outcome == "review"
        assert result.application_id is None

        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                "select kind, candidates from review_items where evidence_ref = $1", "msg-x"
            )
        assert row["kind"] == "ambiguous_match"
        assert len(row["candidates"]) == 2

    async def test_a_cancelled_interview_is_asked_about_once(
        self, db: Database, user_id: str
    ) -> None:
        from loop.domain.messages import CalendarInvite

        await _clear_queues(db)
        result = await ResolverService(db).resolve(
            signal(
                user_id,
                intent="interview_cancelled",
                invite=CalendarInvite(
                    uid="ev-1", summary="Technical", starts_at=NOW, method="CANCEL"
                ),
            )
        )
        async with db.session(user_id) as connection:
            kinds = await connection.fetch(
                "select kind, excerpt from review_items where user_id = $1", user_id
            )
        assert [r["kind"] for r in kinds] == ["unknown_intent"]
        assert "Rescheduling" in kinds[0]["excerpt"]
        # The stage claim is withdrawn automatically; only the question is asked.
        assert result.events == 1


class TestTheWholeDownstreamHalf:
    async def test_a_signal_becomes_a_projected_application(
        self, db: Database, user_id: str
    ) -> None:
        await _clear_queues(db)
        resolved = await ResolverService(db).resolve(
            signal(user_id, intent="interview_invite", stage_hint=None)
        )
        assert resolved.application_id is not None

        pipeline = PipelineService(db)
        for message in await claim(db, Queue.EVENT, batch=10, visibility=30):
            await pipeline.handle(message)

        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                "select current_stage, current_phase, status from applications where id = $1",
                resolved.application_id,
            )
        # An invitation whose title named no round: interviewing, and no claim
        # about which interview it was.
        assert row["current_phase"] == "interviewing"
        assert row["current_stage"] == "interview"
        assert row["status"] == "live"
