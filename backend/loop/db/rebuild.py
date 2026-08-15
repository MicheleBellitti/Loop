"""Drop the projection and re-derive it from the log.

"`applications` can be dropped and rebuilt from `application_events` by one
function; a test asserts the rebuild is byte-identical" (Spec §04). This is that
function, and it is the reason the extractor can be improved next month and last
month's history re-read rather than being stuck with whatever the rules said at
the time.

The blanking is the point. `project_application` writes `role_title`,
`seniority`, `location` and `work_mode` with `coalesce($n, column)`, so a rebuild
that left the old values in place would produce an identical row whether or not
the fold could actually derive them — a test that passes because nothing was
tested. Every derived column is reset to what a row looks like before its first
event, and only then re-folded.

Which leaves the question of what "derived" means, and the port answered it
twice where the reference had left it ambiguous. The reference blanked
`seniority` but the resolver only ever wrote it to the row, never to an event —
so a rebuild silently dropped it. Same for a hand-added application's
`location`. Both now travel in the event payload, so the claim this module makes
is true rather than nearly true.
"""

from typing import Any, Final

import asyncpg

from .events import project_application

# Everything a rebuild compares. `role_embedding` is excluded because it is a
# 384-dimensional float vector recomputed by the resolver, not the fold. The
# complement — identity, the company the resolver decided on, that embedding,
# and the flags recording how the row came to exist — is deliberately not
# written out a second time: a list nothing reads is a comment that goes stale
# without a test noticing.
_SNAPSHOT_COLUMNS: Final = """
    id, company_id, role_title, seniority, location, work_mode,
    current_stage, current_phase, status, applied_at, last_signal_at,
    went_dormant_at, last_user_action_at, awaiting_them, presumed_closed,
    comp_expectation_minor, comp_currency, confidence, needs_review
"""


async def reset_projection(connection: asyncpg.Connection, application_id: str) -> None:
    """A row as it looks before its first event has been folded.

    `role_title` is `not null`, so it is emptied rather than nulled; the fold's
    `coalesce` puts the real one back and an empty string afterwards means the
    log never carried one.
    """
    await connection.execute(
        """
        update applications set
          role_title = '',
          seniority = null,
          location = null,
          work_mode = null,
          current_stage = 'applied',
          current_phase = 'sent',
          status = 'live',
          applied_at = null,
          last_signal_at = null,
          went_dormant_at = null,
          last_user_action_at = null,
          awaiting_them = true,
          presumed_closed = false,
          comp_expectation_minor = null,
          comp_currency = null,
          confidence = 1.0,
          needs_review = false
        where id = $1
        """,
        application_id,
    )


async def rebuild_application(
    connection: asyncpg.Connection, user_id: str, application_id: str
) -> bool:
    """Reset and re-fold one application. False means there was nothing to fold.

    The guard is not defensive tidiness, it is the difference between a rebuild
    and a deletion. `project_application` returns early when an application has
    no events, so blanking first and folding second would leave such a row at
    `role_title = ''`, `applied_at = null`, `confidence = 1.0` with nothing to
    put it back. Rows in that state are ordinary, not corrupt: `quick_add` and
    the resolver both insert the row and *publish* the first event, so every
    application is event-less for the moment before the pipeline consumes it,
    and a row merged away by `_merge_a_duplicate` never receives one at all.
    """
    has_events = await connection.fetchval(
        "select exists (select 1 from application_events where application_id = $1)",
        application_id,
    )
    if not has_events:
        return False
    await reset_projection(connection, application_id)
    await project_application(connection, user_id, application_id)
    return True


async def rebuild_all(connection: asyncpg.Connection, user_id: str) -> int:
    """Every application this user has, in id order.

    Returns how many were rebuilt, which is not necessarily how many exist —
    an application with no events yet is left exactly as it is.
    """
    rows = await connection.fetch(
        "select id from applications where user_id = $1 order by id", user_id
    )
    rebuilt = 0
    for row in rows:
        if await rebuild_application(connection, user_id, str(row["id"])):
            rebuilt += 1
    await refresh_projections(connection)
    return rebuilt


async def refresh_projections(connection: asyncpg.Connection) -> None:
    """The materialised view behind the funnel.

    `app_phase_reach` is what every phase-reach ratio counts, and it is only as
    current as the last refresh. Migration 007 made the function security
    definer precisely so a service that does not own the view may call it.
    """
    await connection.execute("select refresh_projections()")


async def snapshot_applications(
    connection: asyncpg.Connection, user_id: str, only: list[str] | None = None
) -> list[dict[str, Any]]:
    """The comparable shape a rebuild test snapshots.

    `only` narrows it to specific applications. The invariant is "the projection
    the pipeline maintained incrementally equals the projection rebuilt from the
    log alone", so the comparison has to be scoped to rows that were actually
    maintained — a row nobody ever projected differs from its rebuild for a
    reason that has nothing to do with the fold.
    """
    rows = await connection.fetch(
        f"""
        select {_SNAPSHOT_COLUMNS}
          from applications
         where user_id = $1
           and ($2::uuid[] is null or id = any($2))
         order by id
        """,
        user_id,
        only,
    )
    return [dict(row) for row in rows]
