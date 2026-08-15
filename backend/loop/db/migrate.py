"""Numbered `.sql` files, run in order, once each.

Deliberately not Alembic. This schema's hardest parts are policies, grants and a
trigger, all of which are only expressible as SQL, and a migration DSL puts a
translation layer over the exact statements a reviewer needs to read literally.
Sixty lines of runner is the cheaper half of that trade.
"""

import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import asyncpg

from loop.paths import migrations_dir

# Arbitrary and stable. Two services booting at once must not both try to
# create the same type.
_LOCK_KEY = 819660201

_NUMBERED = re.compile(r"^(\d+)_")


class MigrationError(Exception):
    """Never swallowed: a schema that is not what the code expects is not a
    condition to carry on from."""


@dataclass(slots=True)
class MigrationResult:
    applied: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)


def default_migrations_dir() -> Path:
    """`backend/migrations/`, which is where they live now.

    They spent the port under `packages/db/migrations/`, shared with the
    TypeScript, because the two implementations talked to the same database and
    two sources of truth for a constraint is how a differential stops meaning
    anything. There is one implementation now.
    """
    return migrations_dir()


def migrations_in(directory: Path) -> list[Path]:
    files = sorted(p for p in directory.iterdir() if p.suffix == ".sql")
    unnumbered = [p.name for p in files if not _NUMBERED.match(p.name)]
    if unnumbered:
        raise MigrationError(f"migrations must start with a number: {', '.join(unnumbered)}")
    return files


async def migrate(
    connection: asyncpg.Connection, directory: Path | None = None
) -> MigrationResult:
    """Apply what has not been applied, under an advisory lock.

    Each file runs in its own transaction, so a failure leaves the schema at the
    last complete migration rather than half way through one. Applied files are
    recorded with a hash of their contents: editing one that has already run is
    an error rather than a no-op, because the database and the file would then
    disagree about what the schema is and nothing would say so.
    """
    result = MigrationResult()
    await connection.execute("select pg_advisory_lock($1)", _LOCK_KEY)
    try:
        await connection.execute(
            """
            create table if not exists schema_migrations (
              name       text primary key,
              sha256     text not null,
              applied_at timestamptz not null default now()
            )
            """
        )
        seen = {
            row["name"]: row["sha256"]
            for row in await connection.fetch("select name, sha256 from schema_migrations")
        }

        for path in migrations_in(directory or default_migrations_dir()):
            body = path.read_text(encoding="utf-8")
            digest = sha256(body.encode("utf-8")).hexdigest()
            previous = seen.get(path.name)

            if previous is not None:
                if previous != digest:
                    raise MigrationError(
                        f"{path.name} has changed since it was applied. Migrations are "
                        "immutable once run; add a new one instead."
                    )
                result.already_applied.append(path.name)
                continue

            async with connection.transaction():
                await connection.execute(body)
                await connection.execute(
                    "insert into schema_migrations (name, sha256) values ($1, $2)",
                    path.name,
                    digest,
                )
            result.applied.append(path.name)
    finally:
        await connection.execute("select pg_advisory_unlock($1)", _LOCK_KEY)
    return result
