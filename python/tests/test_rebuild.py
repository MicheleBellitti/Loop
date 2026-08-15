"""The projection is derived, and this is what makes that a fact.

"`applications` can be dropped and rebuilt from `application_events` by one
function; a test asserts the rebuild is byte-identical" (Spec §04).

The blanking is the whole test. `project_application` writes four columns with
`coalesce($n, column)`, so a rebuild that left the old values sitting there would
produce an identical row whether or not the log could actually derive them —
which is a test that passes because nothing was tested. Every case below resets
first, and two of them exist because doing that honestly turned up two columns
the reference could not in fact rebuild.
"""

from datetime import UTC, datetime, timedelta

import pytest

from loop.db import (
    Database,
    append_event,
    project_application,
    rebuild_all,
    rebuild_application,
    reset_projection,
    snapshot_applications,
)
from loop.domain.messages import PendingEvent

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 12, 8, 0, tzinfo=UTC)

# The bundle's worked example: an acknowledgement at 0.99 followed by two stage
# changes below it. The literal §05 rule orders by confidence and can never get
# past the acknowledgement.
HISTORY = [
    ("applied", "applied", 1.0, 4, 0),
    ("acknowledged", "acknowledged", 0.99, 1, 1),
    ("stage_advanced", "hr_call", 0.95, 1, 19),
    ("stage_advanced", "onsite_loop", 0.90, 2, 42),
]


async def _application(db: Database, user_id: str) -> str:
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
                  (user_id, company_id, role_title, seniority, location, work_mode,
                   current_stage, current_phase, confidence)
                values ($1,$2,'Platform Engineer','senior','Milan','hybrid',
                        'applied','sent',0.9)
                returning id
                """,
                user_id,
                company,
            )
        )


async def _with_history(db: Database, user_id: str, application_id: str) -> None:
    async with db.session(user_id) as connection:
        for index, (type_, stage, confidence, rung, day) in enumerate(HISTORY):
            await append_event(
                connection,
                PendingEvent(
                    user_id=user_id,
                    application_id=application_id,
                    type=type_,  # type: ignore[arg-type]
                    occurred_at=NOW + timedelta(days=day),
                    confidence=confidence,
                    to_stage=stage,
                    rung=rung,  # type: ignore[arg-type]
                    evidence_ref=f"m-{index}",
                    payload={
                        "role_title": "Platform Engineer",
                        "seniority": "senior",
                        "location": "Milan",
                        "work_mode": "hybrid",
                    },
                ),
            )
        await project_application(connection, user_id, application_id)


class TestTheRebuild:
    async def test_is_byte_identical_after_a_drop_and_rebuild(
        self, db: Database, user_id: str
    ) -> None:
        application_id = await _application(db, user_id)
        await _with_history(db, user_id, application_id)

        async with db.session(user_id) as connection:
            before = await snapshot_applications(connection, user_id, [application_id])
            await rebuild_all(connection, user_id)
            after = await snapshot_applications(connection, user_id, [application_id])

        assert after == before
        # And the fold advanced past the 0.99 acknowledgement, which the literal
        # §05 rule could not have done.
        assert after[0]["current_stage"] == "onsite_loop"
        assert after[0]["current_phase"] == "interviewing"

    async def test_the_reset_actually_blanks_the_row_first(
        self, db: Database, user_id: str
    ) -> None:
        # Without this the test above proves nothing: a row that was never
        # cleared matches its own rebuild by construction.
        application_id = await _application(db, user_id)
        await _with_history(db, user_id, application_id)

        async with db.session(user_id) as connection:
            await reset_projection(connection, application_id)
            blanked = await snapshot_applications(connection, user_id, [application_id])

        assert blanked[0]["current_stage"] == "applied"
        assert blanked[0]["role_title"] == ""
        assert blanked[0]["seniority"] is None
        assert blanked[0]["location"] is None
        assert blanked[0]["applied_at"] is None

    async def test_rebuilds_the_four_columns_the_projection_coalesces(
        self, db: Database, user_id: str
    ) -> None:
        # `role_title`, `seniority`, `location` and `work_mode` survive a normal
        # projection because it coalesces them. After a reset they can only come
        # back from the log — and `seniority` is the one the reference could not
        # bring back, because the resolver wrote it to the row and never to an
        # event.
        application_id = await _application(db, user_id)
        await _with_history(db, user_id, application_id)

        async with db.session(user_id) as connection:
            await rebuild_application(connection, user_id, application_id)
            row = await snapshot_applications(connection, user_id, [application_id])

        assert row[0]["role_title"] == "Platform Engineer"
        assert row[0]["seniority"] == "senior"
        assert row[0]["location"] == "Milan"
        assert row[0]["work_mode"] == "hybrid"

    async def test_counts_what_it_rebuilt(self, db: Database, user_id: str) -> None:
        application_id = await _application(db, user_id)
        await _with_history(db, user_id, application_id)
        async with db.session(user_id) as connection:
            assert await rebuild_all(connection, user_id) == 1

    async def test_refreshes_the_view_the_funnel_reads(
        self, db: Database, user_id: str
    ) -> None:
        # `rebuild_all` ends with `refresh_projections()`. If the grant from
        # migration 007 were wrong this raises, which is how it was found the
        # first time.
        application_id = await _application(db, user_id)
        await _with_history(db, user_id, application_id)
        async with (
            Database(db.dsn, role="loop_pipeline") as scoped,
            scoped.session(user_id) as connection,
        ):
            await rebuild_all(connection, user_id)
            reached = await connection.fetchrow(
                "select reached_interview from app_phase_reach where id = $1",
                application_id,
            )
        # The view is current, and it agrees with the fold: `onsite_loop` is an
        # interviewing stage, so this application reached interview.
        assert reached is not None and reached["reached_interview"] is True
