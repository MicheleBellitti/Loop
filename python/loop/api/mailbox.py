"""Is it still reading my mail?

The one question the product has to answer honestly, and it is asked from two
places — the Today card and the shell's own poll — so it is answered in one.

That is not tidiness. The shell branches on `state`: `F1` full-screens "access
revoked", and an empty `providers` list sends the user to onboarding. Two
readings of the same rows would eventually disagree, and the disagreement would
be a person told their mailbox was fine on one screen and disconnected on the
next.

Three of the numbers here are less obvious than they look.

**Freshness is the worst provider, not the best.** A mailbox and a calendar are
both connected; if the calendar has not synced in a week, that is the truth
about this account even though the mail came in a minute ago. Taking the newest
would report the account as healthy precisely when half of it had stopped.

**`connected` means connected and usable.** An account whose grant was revoked
still has a row, so a row count alone says yes to a mailbox that cannot be read.

**A backlog is a state, not a statistic.** `F2` is the strip that says "still
catching up", and it is what stops a half-scanned mailbox from looking like a
finished one with very little in it.
"""

from datetime import UTC, datetime
from typing import Any, Final

import asyncpg

from .serialise import iso_z

_SECONDS_PER_MINUTE: Final = 60

_ACCOUNTS = """
select provider, status, last_ok_at, backlog_estimate
  from mailbox_accounts where user_id = $1 order by created_at
"""

# Counted from the replay log rather than from the event log: what the user is
# being told is how much mail was read today, and most mail produces no event.
_PLACED_TODAY = """
select count(*) from seen_messages
 where user_id = $1 and outcome = 'placed'
   and processed_at > date_trunc('day', now())
"""


async def mailbox_health(connection: asyncpg.Connection, user_id: str) -> dict[str, Any]:
    rows = await connection.fetch(_ACCOUNTS, user_id)
    placed_today = await connection.fetchval(_PLACED_TODAY, user_id)

    seen_at = [row["last_ok_at"] for row in rows if row["last_ok_at"]]
    worst = min(seen_at) if seen_at else None
    backlog = sum(row["backlog_estimate"] for row in rows)
    needs_reauth = any(row["status"] == "needs_reauth" for row in rows)

    return {
        "connected": bool(rows) and not needs_reauth,
        "providers": [
            {
                "provider": row["provider"],
                "status": row["status"],
                "last_ok_at": iso_z(row["last_ok_at"]),
            }
            for row in rows
        ],
        "last_ok_at": iso_z(worst),
        "minutes_since_read": _minutes_since(worst),
        "placed_today": int(placed_today or 0),
        "backlog": backlog,
        # F1 needs a full screen; F2 is a strip.
        "state": "F1" if needs_reauth else ("F2" if backlog > 0 else "ok"),
    }


def _minutes_since(moment: datetime | None) -> int | None:
    if moment is None:
        return None
    return int((datetime.now(UTC) - moment).total_seconds() // _SECONDS_PER_MINUTE)
