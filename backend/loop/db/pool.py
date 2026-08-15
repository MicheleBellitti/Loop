"""The connection pool, and the one way to talk to a tenant's rows.

Every table in this schema has row-level security with FORCE, and every policy
reads `loop.user_id` from the session. A query that forgets to set it returns
nothing at all — which is the right failure, and still a failure. So there is
exactly one way to get a connection that can see a user's data, and it takes the
user id.

`set_config(..., true)` is transaction-local, and outside a transaction Postgres
scopes it to the single statement that follows. Session-scoped configuration
would leak across a pooled connection to the next tenant. Both facts point the
same way: a tenant session *is* a transaction, and making that the only shape
available is what stops the two from being got wrong separately.

The other half is the role. A superuser — which the owner is — bypasses
row-level security entirely, policies and FORCE alike, so a service connecting
as the owner has no tenant isolation at all however carefully the policies are
written. `set local role` drops the transaction to the service's own role, which
is also what makes the grants mean something: only `loop_pipeline` can insert an
event, and that is the single-writer rule enforced by the database rather than
by everyone remembering it.
"""

import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from types import TracebackType
from typing import Final, Self
from uuid import UUID

import asyncpg

# Long enough for the queries either side of a unit of work, and short enough
# that a connection held open by mistake is noticed. The extraction ladder runs
# outside any of this, so no inference time is ever inside the window.
IDLE_IN_TRANSACTION_TIMEOUT_MS = 30_000


class _FromEnv:
    """Distinguishes "not told which role" from "told: no role at all"."""


_FROM_ENV: Final = _FromEnv()


class Database:
    """An asyncpg pool with this schema's conventions applied once."""

    def __init__(
        self,
        dsn: str,
        *,
        role: str | _FromEnv | None = _FROM_ENV,
        min_size: int = 1,
        max_size: int = 10,
        idle_in_transaction_timeout_ms: int = IDLE_IN_TRANSACTION_TIMEOUT_MS,
    ) -> None:
        self._dsn = dsn
        # An open transaction with nothing to do holds its locks and pins the
        # vacuum horizon. Only a caller that waits on something slow inside one
        # has any reason to raise this, and it must not raise it for everybody
        # else — which is what the reference did, coupling the timeout to the
        # model's, and is the coupling `loop.ladder` removed by running outside
        # any transaction at all. Nothing in this codebase raises it today; the
        # argument exists so that a caller who needs to does not edit a constant
        # every other caller reads.
        self._idle_in_transaction_timeout_ms = idle_in_transaction_timeout_ms
        # Each service runs as its own role, named by `DB_ROLE` per container.
        # Omitting the argument reads that; passing `role=None` means the owner,
        # explicitly, and the two are not the same thing.
        #
        # They used to be. `role if role is not None else os.environ.get(...)`
        # made `role=None` indistinguishable from saying nothing, so the one
        # caller that asks for the owner by name — and comments on why — got
        # `DB_ROLE` instead, which compose sets for every container including
        # the gateway's.
        self._role = os.environ.get("DB_ROLE") if isinstance(role, _FromEnv) else role
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            init=_prepare_connection,
            server_settings={
                "idle_in_transaction_session_timeout": str(
                    self._idle_in_transaction_timeout_ms
                ),
                "application_name": "loop",
            },
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def dsn(self) -> str:
        """For the one caller that cannot use the pool.

        A `listen` holds its connection for as long as it is subscribed, and a
        pooled connection is handed to the next caller the moment it is
        released — so the nudge service opens its own.
        """
        return self._dsn

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("the pool is not open; use `async with Database(dsn)`")
        return self._pool

    @asynccontextmanager
    async def session(
        self, user_id: str, *, read_only: bool = False
    ) -> AsyncIterator[asyncpg.Connection]:
        """A transaction that can see one user's rows, and only theirs.

        Short by construction. Nothing that waits on a network — a model, an
        API, a queue poll — belongs inside one: the connection is held for the
        whole block, and a pool of ten is exhausted by ten slow calls.

        `read_only=True` adds `set transaction read only`, which the reference's
        `withUserReadOnly` opened every read route with and which the port had
        dropped. It is not redundant with the grants: migration 014 gives
        `loop_gateway` real INSERT on `companies` and `applications`, because
        quick add is the one place the user rather than the mailbox is the
        source of truth — so "a GET does not write" is a convention here, and
        this is the line that makes the database enforce it instead. Free: a
        read-write transaction that never writes costs exactly the same.

        It must be the first statement in the transaction — Postgres refuses
        `set transaction` once the transaction has done anything — which is why
        it goes before the role and the tenant.
        """
        async with self.pool.acquire() as connection, connection.transaction():
            if read_only:
                await connection.execute("set transaction read only")
            if self._role:
                await connection.execute(f"set local role {_quoted(self._role)}")
            await connection.execute(
                "select set_config('loop.user_id', $1, true)", str(user_id)
            )
            yield connection

    @asynccontextmanager
    async def untenanted(self) -> AsyncIterator[asyncpg.Connection]:
        """For the queue and the migrations, which are not a user's rows.

        Named awkwardly on purpose: reaching for this to read application data
        is how a previous measurement came to count another tenant's
        integration-test rows as this mailbox's.

        **No role, and that is load-bearing rather than an omission.** The
        gateway's whole sign-in path runs through here — `sole_user`, the
        credential list behind `/api/auth/login/options`, the challenge — and
        every one of those happens before there is a tenant to establish. The
        policies on `users`, `credentials` and `auth_secrets` are FORCEd and read
        `loop.user_id`, so as `loop_gateway` those reads return zero rows rather
        than an error: nobody could sign in, and nothing would say why. Adding
        `set local role` here to match `session` would look like hardening and
        would lock everyone out of the product.
        """
        async with self.pool.acquire() as connection:
            yield connection


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$", re.IGNORECASE)


def _quoted(name: str) -> str:
    """An identifier cannot be a bind parameter, so this is the only place one
    is built — and it refuses anything that is not plainly a role name."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"unsafe role name: {name}")
    return f'"{name}"'


def _default(value: object) -> str:
    """The two types a payload carries that JSON has no room for.

    Datetimes in ISO form, because `str(datetime)` puts a space where the
    separator goes and these payloads are read by a TypeScript implementation as
    well as this one for as long as both run. UUIDs because asyncpg returns one
    for every uuid column, so any payload built from a row that was read back —
    an application id, an interview id — carries them.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def _dumps(value: object) -> str:
    return json.dumps(value, default=_default)


async def _prepare_connection(connection: asyncpg.Connection) -> None:
    """Codecs this schema needs on every connection.

    asyncpg hands `jsonb` back as text unless told otherwise, and an event
    payload is read as a mapping everywhere.

    Note what this means for callers: with a codec installed, a parameter bound
    to a `jsonb` column is a **dict**, never a string. Passing `json.dumps(...)`
    encodes it twice and stores a JSON string that reads back as one — which is
    a bug no type checker catches and a defensive `json.loads` on the way out
    hides completely.

    `vector` is deliberately left as text: pgvector's wire format would be one
    more dependency, the column is always read with an explicit `::text` cast,
    and written by casting a literal back.
    """
    for name in ("json", "jsonb"):
        await connection.set_type_codec(
            name, encoder=_dumps, decoder=json.loads, schema="pg_catalog"
        )
