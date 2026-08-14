"""Is it still reading my mail?

The one question the product has to answer honestly, and it is asked from two
places — the Today card and the shell's own poll — so it is answered in one.

That is not tidiness. The shell branches on `state`: `F1` full-screens "access
revoked", and an empty `providers` list sends the user to onboarding. Two
readings of the same rows would eventually disagree, and the disagreement would
be a person told their mailbox was fine on one screen and disconnected on the
next.

Four of the numbers here are less obvious than they look.

**Freshness is the worst provider, not the best.** Two mailboxes are connected;
if one has not synced in a week, that is the truth about this account even
though the other came in a minute ago. Taking the newest would report the
account as healthy precisely when half of it had stopped. A provider that has
*never* read is the worst freshness there is rather than an absent one, so a
null cannot be filtered out on the way to the minimum — dropping it answers
with the healthy half, which is the same bug wearing a different hat.

**`connected` means connected and usable.** An account whose grant was revoked
still has a row, so a row count alone says yes to a mailbox that cannot be
read. Nor is revocation the only way to be unreadable: the schema allows
`ok | needs_reauth | error | paused`, and the connector writes `error` on every
failure that is not an auth failure. Anything but `ok` is a provider that is
not currently reading, so `connected` asks for at least one that is.

**`F1` is the whole screen, so it takes every provider to earn it.** The shell
replaces the entire product with "access revoked" on `F1`. One lapsed grant out
of two is a degraded account, not an unreachable one — its status rides in the
`providers` entry instead, and the full screen is reserved for the case where
nothing at all can be read.

**A backlog is a state, not a statistic.** `F2` is the strip that says "still
catching up", and it is what stops a half-scanned mailbox from looking like a
finished one with very little in it.

A note on the shape of the rows, because it surprises people reading the
freshness rule: this port syncs a Google account's calendar against the *mail*
row's token (`loop.services.connector`), so one Google account is one row here,
not the two the TypeScript gateway wrote. The worst-provider rule is still
load-bearing — the schema keys on `(user_id, provider, address)`, so a second
mailbox is a second row — it simply is not a mail-and-calendar pair.

Two deliberate divergences from the reference, both in the `providers` entries.
`id` is here because `DELETE /api/mailboxes/{id}` is the only thing that hands
the OAuth grant back to Google, and no other route hands out an id — without it
that endpoint is unreachable from any client. `address` is here because the
schema permits two accounts of one provider and the shell keys a list on them.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg

from loop.domain.clock import minutes_between

from .serialise import iso_z

_log = logging.getLogger("loop.api.mailbox")

_ACCOUNTS = """
select id, provider, address, status, last_ok_at, backlog_estimate
  from mailbox_accounts where user_id = $1 order by created_at, id
"""

# Counted from the replay log rather than from the event log: what the user is
# being told is how much mail was read today, and most mail produces no event.
#
# "Today" is the user's day, not the server's. The instant arrives as a
# parameter rather than as `now()` because the caller is inside a transaction,
# where `now()` is the time the transaction opened, and because a day boundary
# nobody can pin is a day boundary nobody can test.
_PLACED_TODAY = """
select count(*) from seen_messages
 where user_id = $1 and outcome = 'placed'
   and processed_at > date_trunc('day', $2::timestamptz at time zone $3) at time zone $3
"""


async def mailbox_health(
    connection: asyncpg.Connection,
    user_id: str,
    *,
    tz: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if tz is None:
        tz = await connection.fetchval("select tz from users where id = $1", user_id)
    rows = await connection.fetch(_ACCOUNTS, user_id)
    placed_today = await _placed_today(connection, user_id, now, tz or "UTC")

    seen_at = [row["last_ok_at"] for row in rows]
    worst = min(seen_at) if seen_at and all(seen_at) else None
    backlog = sum(row["backlog_estimate"] for row in rows)
    statuses = [row["status"] for row in rows]
    revoked = bool(rows) and all(status == "needs_reauth" for status in statuses)

    return {
        "connected": any(status == "ok" for status in statuses),
        "providers": [
            {
                # The id is what `DELETE /api/mailboxes/{id}` is addressed with,
                # and the address is the only thing telling two accounts of the
                # same provider apart.
                "id": str(row["id"]),
                "provider": row["provider"],
                "address": row["address"],
                "status": row["status"],
                "last_ok_at": iso_z(row["last_ok_at"]),
            }
            for row in rows
        ],
        "last_ok_at": iso_z(worst),
        "minutes_since_read": _minutes_since(worst, now),
        "placed_today": placed_today,
        "backlog": backlog,
        # F1 needs a full screen; F2 is a strip.
        "state": "F1" if revoked else ("F2" if backlog > 0 else "ok"),
    }


async def _placed_today(
    connection: asyncpg.Connection, user_id: str, now: datetime, tz: str
) -> int:
    """A statistic that must never take the auth signal down with it.

    The shell decides whether to full-screen "access revoked" from this same
    object, so a count that fails has to be a count reported as zero rather than
    a request that fails — the alternative is a user whose grant really is
    revoked being shown a normal dashboard of frozen data.

    The savepoint is what makes that possible: the caller is already inside a
    transaction, where a failed statement poisons everything after it.
    """
    try:
        async with connection.transaction():
            return int(await connection.fetchval(_PLACED_TODAY, user_id, now, tz))
    except asyncpg.PostgresError:
        _log.warning("could not count today's placements", exc_info=True)
        return 0


def _minutes_since(moment: datetime | None, now: datetime) -> int | None:
    if moment is None:
        return None
    # The row is stamped by the database's clock and read against this process's,
    # so a second of skew must not reach the client as "-1 minutes ago".
    return max(0, minutes_between(moment, now))
