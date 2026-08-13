"""Appending to the log, and folding it back into the row.

Only the pipeline holds the grants that let these statements run — the database
enforces the single-writer rule, so importing this elsewhere fails loudly rather
than quietly corrupting state.
"""

import asyncpg

from loop.domain import StageTable, fold
from loop.domain.messages import PendingEvent
from loop.domain.stages import UNSPECIFIED_INTERVIEW
from loop.domain.types import DomainEvent, EventType, StageDef

# Stages where the ball is in the user's court.
#
# The follow-up rule needs "awaiting them", which the spec names but never
# derives. A take-home is waiting on you by definition; so is an offer you have
# not answered. Everything else is waiting on them.
_USER_OWES_A_MOVE = frozenset({"take_home", "offer", "negotiating"})

_HUMAN_AUTHORED: frozenset[EventType] = frozenset(
    {"human_corrected", "note_added", "withdrawn", "accepted"}
)

_HUMAN_RUNG = 4


def to_domain_event(row: asyncpg.Record) -> DomainEvent:
    payload = row["payload"]
    return DomainEvent(
        id=str(row["id"]),
        type=row["type"],
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        from_stage=row["from_stage"],
        to_stage=row["to_stage"],
        payload=payload or {},
        confidence=float(row["confidence"]),
        evidence_ref=row["evidence_ref"],
        rung=row["rung"],
    )


async def append_event(connection: asyncpg.Connection, event: PendingEvent) -> str | None:
    """Idempotent by construction.

    The unique index on `(application_id, type, occurred_at, evidence_ref)` with
    NULLS NOT DISTINCT means the same queue message delivered twice produces one
    row. Returns None when the event was already there, which is how the caller
    knows not to notify a second time — a redelivery must not buzz a phone
    again.
    """
    event_id = await connection.fetchval(
        """
        insert into application_events
          (application_id, user_id, type, occurred_at, from_stage, to_stage,
           payload, source_id, confidence, evidence_ref, rung)
        values ($1,$2,$3,$4,$5,$6,$7,null,$8,$9,$10)
        on conflict do nothing
        returning id
        """,
        event.application_id,
        event.user_id,
        event.type,
        event.occurred_at,
        event.from_stage,
        event.to_stage,
        event.payload,
        event.confidence,
        event.evidence_ref,
        event.rung,
    )
    return str(event_id) if event_id is not None else None


async def load_events(connection: asyncpg.Connection, application_id: str) -> list[DomainEvent]:
    rows = await connection.fetch(
        "select * from application_events where application_id = $1 order by occurred_at, id",
        application_id,
    )
    return [to_domain_event(row) for row in rows]


async def load_stage_table(connection: asyncpg.Connection, user_id: str) -> StageTable:
    """The user's own stage set, because it is editable.

    Falls back to the defaults when a user has none rather than raising: a
    missing seed should degrade to the standard pipeline, not to no pipeline.
    """
    rows = await connection.fetch(
        """
        select key, label, phase, depth, stale_after_days
          from stage_defs where user_id = $1 order by depth
        """,
        user_id,
    )
    defs = [
        StageDef(r["key"], r["label"], r["phase"], r["depth"], r["stale_after_days"])
        for r in rows
    ]
    return StageTable(defs) if defs else StageTable()


async def project_application(
    connection: asyncpg.Connection, user_id: str, application_id: str
) -> None:
    """Recompute one application's row from its log alone.

    Every column written here is derived. Drop the row, run this, and the same
    row comes back — which is what lets the extractor be improved next month and
    re-derive last month's history.
    """
    events = await load_events(connection, application_id)
    if not events:
        return
    stages = await load_stage_table(connection, user_id)
    state = fold(events, stages=stages)

    went_dormant_at = None
    presumed_closed = False
    last_user_action_at = None
    for event in events:
        if event.type == "went_silent":
            went_dormant_at = event.occurred_at
            presumed_closed = event.payload.get("presumed_closed") is True
        authored_by_the_user = event.type in _HUMAN_AUTHORED or event.rung == _HUMAN_RUNG
        if authored_by_the_user and (
            last_user_action_at is None or event.occurred_at > last_user_action_at
        ):
            last_user_action_at = event.occurred_at
    if state.status != "dormant":
        went_dormant_at = None
        presumed_closed = False

    open_deadlines = await connection.fetchval(
        """
        select count(*) from deadlines
         where application_id = $1 and met_at is null and due_at > now()
        """,
        application_id,
    )
    unresolved_reviews = await connection.fetchval(
        "select count(*) from review_items where application_id = $1 and resolved_at is null",
        application_id,
    )

    awaiting_them = (
        state.status == "live"
        and state.current_stage not in _USER_OWES_A_MOVE
        and not open_deadlines
    )

    await connection.execute(
        """
        update applications set
          current_stage = $2, current_phase = $3, status = $4,
          applied_at = $5, last_signal_at = $6,
          went_dormant_at = $7, last_user_action_at = $8,
          awaiting_them = $9, presumed_closed = $10,
          role_title = coalesce($11, role_title),
          seniority = coalesce($12, seniority),
          location = coalesce($13, location),
          work_mode = coalesce($14, work_mode),
          comp_expectation_minor = $15, comp_currency = $16,
          confidence = $17, needs_review = $18
        where id = $1
        """,
        application_id,
        state.current_stage,
        state.current_phase,
        state.status,
        state.applied_at,
        state.last_signal_at,
        went_dormant_at,
        last_user_action_at,
        awaiting_them,
        presumed_closed,
        state.role_title,
        state.seniority,
        state.location,
        state.work_mode,
        state.comp_expectation_minor,
        state.comp_currency,
        state.confidence,
        bool(unresolved_reviews),
    )


async def apply_side_effects(
    connection: asyncpg.Connection, user_id: str, application_id: str, event: DomainEvent
) -> None:
    """Satellite rows some events imply. Written by the pipeline only."""
    match event.type:
        case "interview_scheduled":
            await _record_interview(connection, user_id, application_id, event)
        case "interview_held":
            interview_id = event.payload.get("interview_id")
            if interview_id:
                await connection.execute(
                    "update interviews set held = true where id = $1 and user_id = $2",
                    interview_id,
                    user_id,
                )
        case "deadline_set":
            await _record_deadline(connection, user_id, application_id, event)
        case _:
            return


async def _record_interview(
    connection: asyncpg.Connection, user_id: str, application_id: str, event: DomainEvent
) -> None:
    """An interview row, and the one that stops being one.

    Two fixes over the reference. It wrote `stage ?? 'technical'`, which is the
    fifth place that default lived — an unnamed round is now recorded as
    `interview`, which claims only what the invitation proved. And it set
    `cancelled_at = null` on every conflict while nothing anywhere ever set it,
    so a cancellation silently reinstated the interview it was cancelling.
    """
    payload = event.payload
    starts_at = payload.get("starts_at")
    if not starts_at:
        return
    cancelled = payload.get("status") == "cancelled"
    stage = payload.get("stage") or UNSPECIFIED_INTERVIEW

    await connection.execute(
        """
        insert into interviews
          (user_id, application_id, stage, starts_at, ends_at, location,
           calendar_event_id, cancelled_at)
        values ($1,$2,$3,$4,$5,$6,$7,$8)
        on conflict (user_id, calendar_event_id) do update
          set starts_at    = excluded.starts_at,
              ends_at      = excluded.ends_at,
              -- A cancellation says when, not which round: keep what is known.
              stage        = case when $8 is null then excluded.stage else interviews.stage end,
              cancelled_at = $8
        """,
        user_id,
        application_id,
        stage,
        starts_at,
        payload.get("ends_at"),
        payload.get("location"),
        payload.get("calendar_event_id"),
        event.occurred_at if cancelled else None,
    )


async def _record_deadline(
    connection: asyncpg.Connection, user_id: str, application_id: str, event: DomainEvent
) -> None:
    due_at = event.payload.get("due_at")
    if not due_at:
        return
    await connection.execute(
        """
        insert into deadlines (user_id, application_id, kind, due_at, url, source)
        values ($1,$2,$3,$4,$5,$6)
        on conflict do nothing
        """,
        user_id,
        application_id,
        event.payload.get("kind", "take_home"),
        due_at,
        event.payload.get("url"),
        event.payload.get("source", "gmail"),
    )
