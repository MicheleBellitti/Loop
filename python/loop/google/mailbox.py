"""The mailbox row: its secret, its cursor, and what it last managed to do.

The posture is that a plaintext refresh token exists inside the connector
process, for the length of one call, and never in a variable a logger can reach.
Everything below either seals or unseals, or moves the row between the four
states the health line reports.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import asyncpg

from .client import Tokens
from .crypto import Sealed, generate_dek, open_sealed, seal, unwrap_dek, wrap_dek

# `last_error` is shown to the user through the health line, so it is a sentence
# and not a stack trace.
_ERROR_CHARS: Final = 500


@dataclass(frozen=True, slots=True)
class Mailbox:
    id: str
    user_id: str
    provider: str
    address: str
    secret_ciphertext: bytes
    secret_nonce: bytes
    dek_wrapped: bytes
    dek_nonce: bytes
    scopes: tuple[str, ...]
    cursor: dict[str, Any]
    watch_expires_at: datetime | None
    status: str
    last_ok_at: datetime | None


def to_mailbox(row: asyncpg.Record) -> Mailbox:
    return Mailbox(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        provider=row["provider"],
        address=row["address"],
        secret_ciphertext=bytes(row["secret_ciphertext"]),
        secret_nonce=bytes(row["secret_nonce"]),
        dek_wrapped=bytes(row["dek_wrapped"]),
        dek_nonce=bytes(row["dek_nonce"]),
        scopes=tuple(row["scopes"] or ()),
        cursor=row["cursor"] or {},
        watch_expires_at=row["watch_expires_at"],
        status=row["status"],
        last_ok_at=row["last_ok_at"],
    )


class NoRefreshToken(Exception):
    """The authorisation did not come with a way to stay authorised."""


async def store_mailbox(
    connection: asyncpg.Connection,
    *,
    user_id: str,
    provider: str,
    address: str,
    tokens: Tokens,
) -> str:
    """Seal the refresh token and record the account. Returns its id.

    Refuses an authorisation with no refresh token, which is the difference
    between reconnecting a mailbox and destroying one. Google omits the refresh
    token when a grant already exists, and the reference sealed `{}` and wrote
    it over the working secret — a failure that surfaced days later as "mailbox
    has no refresh token" with no trace of what had caused it.

    A fresh data key on every reconnect, and `cursor`, `watch_expires_at` and
    `last_ok_at` deliberately left alone: reconnecting resumes, it does not
    start again.
    """
    if not tokens.refresh_token:
        raise NoRefreshToken(
            "Google returned no refresh token. The consent screen must be "
            "requested with prompt=consent."
        )

    dek = generate_dek()
    wrapped = wrap_dek(dek)
    sealed = seal(json.dumps({"refresh_token": tokens.refresh_token}).encode(), dek)

    return str(
        await connection.fetchval(
            """
            insert into mailbox_accounts
              (user_id, provider, address, secret_ciphertext, secret_nonce,
               dek_wrapped, dek_nonce, scopes, status)
            values ($1,$2,$3,$4,$5,$6,$7,$8,'ok')
            on conflict (user_id, provider, address) do update
              set secret_ciphertext = excluded.secret_ciphertext,
                  secret_nonce      = excluded.secret_nonce,
                  dek_wrapped       = excluded.dek_wrapped,
                  dek_nonce         = excluded.dek_nonce,
                  scopes            = excluded.scopes,
                  status            = 'ok',
                  last_error        = null
            returning id
            """,
            user_id,
            provider,
            address,
            sealed.ciphertext,
            sealed.nonce,
            wrapped.ciphertext,
            wrapped.nonce,
            tokens.scope.split(),
        )
    )


def read_refresh_token(mailbox: Mailbox) -> str:
    dek = unwrap_dek(Sealed(mailbox.dek_wrapped, mailbox.dek_nonce))
    opened = open_sealed(Sealed(mailbox.secret_ciphertext, mailbox.secret_nonce), dek)
    token = json.loads(opened).get("refresh_token")
    if not token:
        raise NoRefreshToken("mailbox has no refresh token")
    return str(token)


async def save_cursor(
    connection: asyncpg.Connection, mailbox_id: str, cursor: dict[str, Any]
) -> None:
    """A shallow merge, not a replace.

    Gmail's history id and the calendar's sync token share one column, and each
    path writes only its own key — so the merge is what keeps them from
    clobbering each other. It depends on the column being `not null default
    '{}'`: against a null, `null || jsonb` is null and the write disappears.
    """
    await connection.execute(
        "update mailbox_accounts set cursor = cursor || $2::jsonb where id = $1",
        mailbox_id,
        cursor,
    )


async def mark_ok(connection: asyncpg.Connection, mailbox_id: str) -> None:
    await connection.execute(
        """
        update mailbox_accounts
           set last_ok_at = now(), status = 'ok', last_error = null
         where id = $1
        """,
        mailbox_id,
    )


async def mark_needs_reauth(
    connection: asyncpg.Connection, mailbox_id: str, reason: str
) -> None:
    """The product's only full-screen failure, so it is set sparingly.

    Everything else degrades: a rate limit slows down, an expired history
    relists, a lapsed watch falls back to polling. This one stops, because it is
    the only failure the system cannot fix without the user.
    """
    await connection.execute(
        "update mailbox_accounts set status = 'needs_reauth', last_error = $2 where id = $1",
        mailbox_id,
        reason[:_ERROR_CHARS],
    )


async def mark_error(connection: asyncpg.Connection, mailbox_id: str, reason: str) -> None:
    await connection.execute(
        "update mailbox_accounts set status = 'error', last_error = $2 where id = $1",
        mailbox_id,
        reason[:_ERROR_CHARS],
    )


async def set_backlog(connection: asyncpg.Connection, mailbox_id: str, waiting: int) -> None:
    await connection.execute(
        "update mailbox_accounts set backlog_estimate = $2 where id = $1",
        mailbox_id,
        waiting,
    )


async def save_watch_expiry(
    connection: asyncpg.Connection, mailbox_id: str, expiration_ms: str
) -> None:
    """Gmail reports the expiry in milliseconds, as a decimal string."""
    await connection.execute(
        "update mailbox_accounts set watch_expires_at = to_timestamp($2::bigint / 1000) "
        "where id = $1",
        mailbox_id,
        expiration_ms,
    )
