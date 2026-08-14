"""The first screen: what changed, what is next, and whether the mailbox is read.

Every key is always present and every string is written here rather than in the
browser — the eyebrow, the headline's three fragments, the weekday on a recent
event. A client that formatted any of them would be a client that had to know
about stages, and the whole point of computing them here is that it does not.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from loop.api import activity_sql, auth
from loop.api.mailbox import mailbox_health
from loop.api.serialise import iso_z
from loop.db import load_stage_table
from loop.domain import build_headline, date_eyebrow
from loop.domain.types import DomainEvent

router = APIRouter(prefix="/api")

_WINDOW_DAYS = 7
_RECENT_LIMIT = 8
_MAX_SUGGESTIONS = 3
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Counted over `activity`, not over `status`. "Live" used to mean "a row we have
# never had a reason to close", which on a twelve-month mailbox is most of them;
# it now means what the word means on the screen it appears on.
_COUNTERS = f"""
with {activity_sql.CTE}
select
  count(*) filter (where activity = 'active') as live,
  count(*) filter (where activity = 'stale') as quiet,
  count(*) filter (where activity <> 'closed'
                     and current_phase = 'interviewing') as interviewing,
  count(*) filter (where activity <> 'closed'
                     and current_stage in ('offer','negotiating')) as offer,
  count(*) filter (where activity = 'stale') as overdue,
  count(*) filter (where activity = 'closed') as closed
from act
"""

_RECENT = """
select e.application_id, c.canonical_name as company, e.type, e.to_stage, e.occurred_at,
       a.status, e.payload
  from application_events e
  join applications a on a.id = e.application_id
  join companies c on c.id = a.company_id
 where e.user_id = $1 and e.occurred_at > now() - interval '7 days'
   and e.type <> 'went_silent'
 order by e.occurred_at desc limit 8
"""

_NEXT_INTERVIEW = """
select i.application_id, c.canonical_name as company, a.role_title, i.stage, i.starts_at,
       (select count(*) from interviews x
         where x.application_id = i.application_id and x.held) as rounds
  from interviews i
  join applications a on a.id = i.application_id
  join companies c on c.id = a.company_id
 where i.user_id = $1 and i.cancelled_at is null and i.starts_at > now()
 order by i.starts_at limit 1
"""

_SUGGESTIONS = """
select key, rule, payload from suggestions
 where user_id = $1 and acted_at is null and dismissed_at is null
   and (snoozed_until is null or snoozed_until < now())
   and (expires_at is null or expires_at > now())
 order by created_at desc limit 3
"""


@router.get("/today")
async def today(request: Request) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    now = datetime.now(UTC)

    async with request.app.state.db.session(session.user_id) as connection:
        tz = (
            await connection.fetchval("select tz from users where id = $1", session.user_id)
            or "UTC"
        )
        stages = await load_stage_table(connection, session.user_id)
        counters = await connection.fetchrow(_COUNTERS, session.user_id)
        window = await connection.fetch(
            """
            select application_id, type, occurred_at, from_stage, to_stage, confidence
              from application_events
             where user_id = $1 and occurred_at > now() - interval '7 days'
            """,
            session.user_id,
        )
        suggestions = await connection.fetch(_SUGGESTIONS, session.user_id)
        recent = await connection.fetch(_RECENT, session.user_id)
        upcoming = await connection.fetchrow(_NEXT_INTERVIEW, session.user_id)
        review_count = await connection.fetchval(
            "select count(*) from review_items where user_id = $1 and resolved_at is null",
            session.user_id,
        )
        health = await mailbox_health(connection, session.user_id, tz=tz, now=now)

    headline = build_headline(
        events=[_as_event(row) for row in window],
        # The application id rides in `evidence_ref` so the headline can count
        # distinct applications without knowing what an application is.
        application_id_of=lambda event: event.evidence_ref or "",
        live_count=counters["live"],
        open_suggestion_count=len(suggestions),
        now=now,
        window_days=_WINDOW_DAYS,
        stages=stages,
    )

    return {
        "eyebrow": date_eyebrow(now, tz),
        "headline": list(headline.lines),
        "headline_kind": headline.kind,
        "counters": {
            "live": counters["live"],
            "quiet": counters["quiet"],
            "interviewing": counters["interviewing"],
            "offer": counters["offer"],
            "overdue": counters["overdue"],
            "closed": counters["closed"],
        },
        "review_count": review_count,
        "next_interview": _next_interview(upcoming, stages),
        "suggestions": [
            {"key": row["key"], "rule": row["rule"], **(row["payload"] or {})}
            for row in suggestions
        ],
        "recent_events": [_recent(row, stages, tz) for row in recent],
        "mailbox_health": health,
        "closing_line": (
            "Everything above was read from your mailbox and calendar. "
            "You have not typed anything this week."
        ),
    }


def _as_event(row: Any) -> DomainEvent:
    return DomainEvent(
        type=row["type"],
        occurred_at=row["occurred_at"],
        confidence=float(row["confidence"]),
        from_stage=row["from_stage"],
        to_stage=row["to_stage"],
        evidence_ref=str(row["application_id"]),
    )


def _next_interview(row: Any, stages: Any) -> dict[str, Any] | None:
    """Null rather than absent when there is nothing scheduled."""
    if row is None:
        return None
    return {
        "application_id": str(row["application_id"]),
        "company": row["company"],
        "role": row["role_title"],
        "stage": stages.label_of(row["stage"]),
        "starts_at": iso_z(row["starts_at"]),
        "rounds_done": row["rounds"],
        "provenance": "from calendar invite",
    }


def _recent(row: Any, stages: Any, tz: str) -> dict[str, Any]:
    from zoneinfo import ZoneInfo

    local = row["occurred_at"].astimezone(ZoneInfo(tz))
    return {
        "application_id": str(row["application_id"]),
        "company": row["company"],
        "what": _what_happened(row, stages),
        # A short weekday, formatted here. The client shows it verbatim.
        "when": _WEEKDAYS[local.weekday()],
        "closed": row["status"] != "live",
    }


def _what_happened(row: Any, stages: Any) -> str:
    payload = row["payload"] or {}
    match row["type"]:
        case "offer_received":
            return "Offer received"
        case "rejected":
            after = payload.get("after_stage")
            return (
                f"Rejected after the {stages.label_of(after).lower()}" if after else "Rejected"
            )
        case "interview_scheduled":
            stage = payload.get("stage")
            label = stages.label_of(stage) if stage else "Interview"
            return f"{label} scheduled"
        case "stage_advanced":
            to = row["to_stage"] or payload.get("to_stage")
            return f"Moved to {stages.label_of(to).lower()}" if to else "Stage changed"
        case "acknowledged":
            return "Application acknowledged"
        case "applied":
            return "Application sent"
        case other:
            return str(other).replace("_", " ")
