"""Create the single user, and print a recovery password once.

    uv run --extra api --extra db python scripts/seed_user.py you@example.com
    uv run --extra api --extra db python scripts/seed_user.py you@example.com --reset

The passkey is enrolled from the browser at first sign-in. This password exists
so there is a way back in when the phone is lost, and it is printed here rather
than mailed because there is no send path in this repository.
"""

import argparse
import asyncio
import os
import secrets

from loop.api.auth import hash_scrypt
from loop.db import Database


def _password() -> str:
    """Long enough not to need to be memorable, short enough to type once."""
    return secrets.token_urlsafe(18)


async def seed(dsn: str, email: str, tz: str, *, reset: bool) -> None:
    async with Database(dsn, role=None) as db, db.untenanted() as connection:
        existing = await connection.fetchrow("select id, email from users limit 1")

        if existing is not None and not reset:
            raise SystemExit(
                "This box already has a user. Loop is single-tenant by design; opening\n"
                "it to a second person is phase 4, which is a different product with a\n"
                "different burden.\n\n"
                "To issue a fresh recovery password for the existing user, pass --reset."
            )

        if existing is not None:
            # A recovery password is single-use by design and a passkey can be
            # lost with the phone that held it. Without this the only way back
            # into your own box was to delete the user and every application
            # with it — a data-loss event dressed up as a password reset.
            if existing["email"].lower() != email.lower():
                raise SystemExit(
                    f"This box belongs to {existing['email']}, not {email}. Refusing to\n"
                    "reset a password for an address that is not the one on record."
                )
            fresh = _password()
            await connection.execute(
                "update auth_secrets"
                " set recovery_hash = $2, recovery_used_at = null where user_id = $1",
                existing["id"],
                hash_scrypt(fresh),
            )
            # Deliberately left alone: this reissues the fallback, it does not
            # revoke credentials that are still working.
            passkeys = await connection.fetchval(
                "select count(*) from credentials where user_id = $1", existing["id"]
            )
            print(_REISSUED.format(email=existing["email"], password=fresh, n=passkeys))
            return

        password = _password()
        user_id = await connection.fetchval(
            "insert into users (email, tz) values ($1, $2) returning id", email, tz
        )
        await connection.execute("select seed_stage_defs($1)", user_id)
        await connection.execute(
            "insert into auth_secrets (user_id, recovery_hash) values ($1, $2)"
            " on conflict (user_id) do update set recovery_hash = excluded.recovery_hash",
            user_id,
            hash_scrypt(password),
        )
        print(_CREATED.format(email=email, tz=tz, password=password))


_CREATED = """
  user created

  email      {email}
  timezone   {tz}

  recovery password

      {password}

  Write it down now — it is hashed, so this is the only time it is shown.
  Sign in with it once, add a passkey, and you will not need it again.
"""

_REISSUED = """
  recovery password reissued for {email}

      {password}

  Shown once. Registered passkeys left untouched ({n} on file).
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("timezone", nargs="?", default=None)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="reissue the recovery password for the user already on this box",
    )
    args = parser.parse_args()

    if "@" not in args.email:
        raise SystemExit(f"{args.email!r} is not an email address")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")

    # The host's zone, which is nearly always the user's — every quiet-hours and
    # dormancy decision is made in it, so a wrong guess is visible immediately
    # and a prompt nobody reads is not.
    tz = args.timezone or _local_zone()
    asyncio.run(seed(dsn, args.email, tz, reset=args.reset))


def _local_zone() -> str:
    from zoneinfo import ZoneInfo

    name = os.environ.get("TZ")
    if name:
        try:
            ZoneInfo(name)
        except Exception:
            name = None
    return name or _from_etc_localtime() or "Europe/Rome"


def _from_etc_localtime() -> str | None:
    """`/etc/localtime` is a symlink into the zoneinfo tree on every Linux."""
    from pathlib import Path

    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        index = parts.index("zoneinfo")
    except (OSError, ValueError):
        return None
    return "/".join(parts[index + 1 :]) or None


if __name__ == "__main__":
    main()
