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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

try:
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - the pure suite runs without it
    asyncpg = None  # type: ignore[assignment]

from loop.db import Database, Queue, migrate

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
        # The queue is one table shared by every test in the run, and several of
        # them publish messages nothing consumes. `erase_user` takes the ones
        # carrying a user id; these are the rest. Without this, a test that
        # claims a batch can pick up a message an earlier test left behind, and
        # what it fails on is unrelated to what it is testing.
        async with database.untenanted() as connection:
            await connection.execute(
                "delete from mq.messages where queue = any($1::text[])",
                [*Queue.ALL, *(f"{queue}_dlq" for queue in Queue.ALL)],
            )


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


@pytest.fixture
async def client(dsn: str, user_id: str) -> AsyncIterator["AsyncClient"]:
    """The API with a session already established, driven in-process."""
    from httpx import ASGITransport, AsyncClient

    from loop.api import Settings, auth, create_app

    app = create_app(Settings(dsn=dsn, session_secret="test-secret"))
    async with app.router.lifespan_context(app):
        token, _session = await app.state.sessions.create(user_id)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={auth.COOKIE_NAME: token},
        ) as http:
            yield http


@pytest.fixture
async def anonymous(dsn: str) -> AsyncIterator["AsyncClient"]:
    from httpx import ASGITransport, AsyncClient

    from loop.api import Settings, create_app

    app = create_app(Settings(dsn=dsn, session_secret="test-secret"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http


@pytest.fixture
async def session_cookie(dsn: str, user_id: str) -> str:
    """A session token, for the tests that talk over a real socket."""
    from loop.api import auth
    from loop.db import Database

    async with Database(dsn, role=None) as db:
        token, _session = await auth.Sessions(db, "test-secret").create(user_id)
    return token


@pytest.fixture
async def served(dsn: str) -> AsyncIterator[str]:
    """The app on a real port, for the one thing an ASGI transport cannot do.

    httpx's in-process transport runs the application to completion before it
    hands back a response, and a server-sent event stream never completes — so
    a test of `/api/stream` through it deadlocks. Everything else uses the
    in-process client, which is faster and needs no port.
    """
    import asyncio
    import contextlib

    import uvicorn

    from loop.api import Settings, create_app

    config = uvicorn.Config(
        create_app(Settings(dsn=dsn, session_secret="test-secret")),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await task


MAILBOX_ADDRESS = "owner@pytest.invalid"

# A fixed instant, so a test that asserts on a date is reading the fixture and
# not the day it happens to run on.
SOME_TUESDAY = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


async def record_message(
    db: Database,
    user_id: str,
    mailbox_id: str,
    *,
    sender: str = "Prima <careers@prima.it>",
    subject: str = "La tua candidatura",
    text: str = "Abbiamo ricevuto la tua candidatura.",
    thread_id: str | None = None,
) -> dict[str, Any]:
    """A message on the wire, plus the replay-log row the connector writes.

    Imported rather than injected as a fixture because two suites want it with
    different arguments in the same test. It stands in for the connector, which
    is the one producer neither of them has.
    """
    provider_message_id = uuid.uuid4().hex
    async with db.session(user_id) as connection:
        await connection.execute(
            """
            insert into seen_messages
              (mailbox_id, provider_message_id, user_id, body_sha256, received_at)
            values ($1,$2,$3,$4,$5)
            """,
            mailbox_id,
            provider_message_id,
            user_id,
            b"\x00" * 32,
            SOME_TUESDAY,
        )
    return {
        "user_id": user_id,
        "mailbox_id": mailbox_id,
        "provider_message_id": provider_message_id,
        "thread_id": thread_id or provider_message_id,
        "received_at": SOME_TUESDAY.isoformat(),
        "headers": {
            "message_id": f"<{provider_message_id}@mail.gmail.com>",
            "from": sender,
            "to": [MAILBOX_ADDRESS],
            "subject": subject,
            "date": "Wed, 30 Jul 2026 09:00:00 +0200",
            "list_id": None,
            "list_unsubscribe": None,
            "precedence": None,
        },
        "text": text,
        "body_sha256": "00" * 32,
        "invite": None,
    }


async def connect_mailbox(
    db: Database,
    user_id: str,
    *,
    provider: str = "gmail",
    address: str = MAILBOX_ADDRESS,
    last_ok_at: datetime | None = None,
) -> str:
    """A connected account.

    Imported rather than injected as a fixture because a test about the *worst*
    of two mailboxes needs it twice, with different arguments, in one test.

    The sealed-secret columns are `not null` and nothing here decrypts them, so
    they hold zeroes: what the tests need is the row and its address, and the
    address is what tells the extractor which half of a thread the user wrote.
    `last_ok_at` starts null, which is what `store_mailbox` writes — a mailbox
    that has been connected and not yet read.
    """
    async with db.session(user_id) as connection:
        return str(
            await connection.fetchval(
                """
                insert into mailbox_accounts
                  (user_id, provider, address, secret_ciphertext, secret_nonce,
                   dek_wrapped, dek_nonce, last_ok_at)
                values ($1,$2,$3,'\\x00','\\x00','\\x00','\\x00',$4)
                returning id
                """,
                user_id,
                provider,
                address,
                last_ok_at,
            )
        )


@pytest.fixture
async def mailbox_id(db: Database, user_id: str) -> str:
    """A connected Gmail account, which `seen_messages` needs to reference."""
    return await connect_mailbox(db, user_id)
