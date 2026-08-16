"""The tools, against real rows.

One thing here is worth a database rather than a fake: which application the
user meant. The model is handed a name, not a UUID — that is the whole point,
because a seven-billion-parameter model asked to carry an id across three turns
invents one — so the lookup is SQL and the SQL is what has to be right.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from loop.chat.tools import Tool, ToolContext, ToolResult, default_tools
from loop.db import Database

pytestmark = pytest.mark.integration


def _tool(name: str) -> Tool:
    return next(tool for tool in default_tools() if tool.name == name)


async def _application(
    db: Database, user_id: str, *, company: str, domain: str, role: str
) -> str:
    async with db.session(user_id) as connection:
        company_id = await connection.fetchval(
            """
            insert into companies (canonical_name, domain) values ($1, $2)
            on conflict (lower(canonical_name), coalesce(domain, '')) do update
              set canonical_name = excluded.canonical_name
            returning id
            """,
            company,
            domain,
        )
        return str(
            await connection.fetchval(
                """
                insert into applications
                  (user_id, company_id, role_title, current_stage, current_phase,
                   confidence, last_signal_at)
                values ($1,$2,$3,'applied','sent',1.0,$4)
                returning id
                """,
                user_id,
                company_id,
                role,
                datetime.now(UTC),
            )
        )


async def _get(db: Database, user_id: str, args: dict[str, Any]) -> ToolResult:
    context = ToolContext(db=db, user_id=user_id, google=None)
    return await _tool("get_application").run(context, args)


class TestNamingAnApplication:
    async def test_the_company_name_is_enough(self, db: Database, user_id: str) -> None:
        application_id = await _application(
            db, user_id, company="Prima", domain="prima.it", role="Staff Engineer"
        )
        result = await _get(db, user_id, {"application": "Prima"})

        assert result.ok
        assert result.payload["id"] == application_id
        assert result.payload["company"] == "Prima"

    async def test_the_role_finds_it_too(self, db: Database, user_id: str) -> None:
        application_id = await _application(
            db, user_id, company="Nexi", domain="nexi.it", role="Backend Engineer"
        )
        result = await _get(db, user_id, {"application": "backend"})

        assert result.ok
        assert result.payload["id"] == application_id

    async def test_a_uuid_still_works(self, db: Database, user_id: str) -> None:
        application_id = await _application(
            db, user_id, company="Satispay", domain="satispay.com", role="SRE"
        )
        # Both spellings of the argument, because a model that has just seen an
        # id reaches for the key that has "id" in it.
        by_name = await _get(db, user_id, {"application": application_id})
        by_key = await _get(db, user_id, {"application_id": application_id})

        assert by_name.ok and by_key.ok
        assert by_name.payload["id"] == by_key.payload["id"] == application_id

    async def test_an_invented_uuid_is_a_plain_no(
        self, db: Database, user_id: str
    ) -> None:
        # What the model used to do, and what it must be told plainly.
        result = await _get(
            db, user_id, {"application_id": "01890000-0000-7000-8000-000000000999"}
        )
        assert not result.ok
        assert "no such application" in result.summary

    async def test_an_unknown_name_points_at_the_listing(
        self, db: Database, user_id: str
    ) -> None:
        result = await _get(db, user_id, {"application": "Acme"})

        assert not result.ok
        assert "list_applications" in result.summary

    async def test_several_matches_come_back_to_choose_from(
        self, db: Database, user_id: str
    ) -> None:
        await _application(
            db, user_id, company="Prima", domain="prima.it", role="Staff Engineer"
        )
        await _application(
            db,
            user_id,
            company="Prima Assicurazioni",
            domain="primassicurazioni.it",
            role="Backend Engineer",
        )
        # "Engineer" is both roles and neither company: nothing to prefer.
        ambiguous = await _get(db, user_id, {"application": "Engineer"})

        assert not ambiguous.ok
        candidates = ambiguous.payload["candidates"]
        assert {row["company"] for row in candidates} == {"Prima", "Prima Assicurazioni"}
        assert all(row["id"] for row in candidates)
        assert "ask again with one of these ids" in ambiguous.summary

    async def test_a_company_named_exactly_wins_over_one_that_merely_contains_it(
        self, db: Database, user_id: str
    ) -> None:
        await _application(
            db, user_id, company="Prima", domain="prima.it", role="Staff Engineer"
        )
        await _application(
            db,
            user_id,
            company="Prima Assicurazioni",
            domain="primassicurazioni.it",
            role="Backend Engineer",
        )
        # "Prima" is a company, not an ambiguity — the longer name is a
        # different employer, and asking which would be pedantic.
        result = await _get(db, user_id, {"application": "prima"})

        assert result.ok
        assert result.payload["company"] == "Prima"

    async def test_nothing_at_all_asks_for_something(
        self, db: Database, user_id: str
    ) -> None:
        result = await _get(db, user_id, {})
        assert not result.ok
        assert "name the application" in result.summary

    async def test_another_user_cannot_be_named(
        self, db: Database, user_id: str
    ) -> None:
        """Row-level security holds under a name as it does under an id."""
        async with db.untenanted() as connection:
            stranger = await connection.fetchval(
                "insert into users (email, tz) values ($1, 'Europe/Rome') returning id",
                "stranger@pytest.invalid",
            )
        try:
            await _application(
                db, str(stranger), company="Bending", domain="bending.dev", role="Dev"
            )
            result = await _get(db, user_id, {"application": "Bending"})
            assert not result.ok
        finally:
            async with db.untenanted() as connection:
                await connection.execute("select erase_user($1)", stranger)
