"""Fixtures for the tests that need a real Postgres.

There is no fake. The things worth testing here — row-level security, an
append-only trigger, the unique index that makes an append idempotent, a queue
built on SKIP LOCKED — are all properties of the database, and a mock of
Postgres would only assert that the mock works.

They run against `DATABASE_URL` rather than a throwaway database, because the
schema cannot be built anywhere else: migration 005 creates `pg_cron`, and
Postgres refuses that outside the one database named in `cron.database_name`.
That is a real deployment constraint, not a test problem, so the tests live with
it — and every row they create belongs to a user they delete afterwards.
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest

try:
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - the pure suite runs without it
    asyncpg = None  # type: ignore[assignment]

from loop.db import Database, migrate

TEST_EMAIL_DOMAIN = "pytest.invalid"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip everything marked `integration` when there is nothing to talk to.

    A hook rather than a decorator each test imports: the condition is a
    property of the run, not of any one test, and `tests/` is deliberately not
    a package.
    """
    if os.environ.get("DATABASE_URL") and asyncpg is not None:
        return
    skip = pytest.mark.skip(reason="set DATABASE_URL and the db extra to run these")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
async def dsn() -> AsyncIterator[str]:
    """The configured database, migrated up to date.

    Migrating is not a side effect of testing here: the Python code needs the
    `interview` stage that migration 012 adds, so a database without it is one
    this port cannot run against at all.
    """
    url = os.environ["DATABASE_URL"]
    connection = await asyncpg.connect(url)
    try:
        await migrate(connection)
    finally:
        await connection.close()
    yield url


@pytest.fixture
async def db(dsn: str) -> AsyncIterator[Database]:
    async with Database(dsn) as database:
        yield database


@pytest.fixture
async def user_id(db: Database) -> AsyncIterator[str]:
    """A user of this test's own, with the default stage set.

    Removed afterwards through `erase_user`, which is the product's own account
    deletion: `application_events` is append-only and the trigger refuses a
    cascade, so a plain `delete from users` fails. Cleaning up through the same
    door the Article 17 path uses means these tests exercise it on every run.
    """
    async with db.untenanted() as connection:
        new_id = await connection.fetchval(
            "insert into users (email, tz) values ($1, 'Europe/Rome') returning id",
            f"{uuid.uuid4().hex}@{TEST_EMAIL_DOMAIN}",
        )
        await connection.execute("select seed_stage_defs($1)", new_id)
    try:
        yield str(new_id)
    finally:
        async with db.untenanted() as connection:
            await connection.execute("select erase_user($1)", new_id)
