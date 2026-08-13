"""The pipeline board.

Seventeen keys per row, all of them always present, and four of them computed
here rather than by the client: the stage label, how long it has been quiet, the
sentence describing that, and the flag. That is the rule the whole design turns
on — no statistic, no stage and no dormancy decision is derived client-side —
and it is what makes a second client cheap later.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from loop.api import auth
from loop.api.serialise import iso_z, num
from loop.db import load_stage_table
from loop.domain import compute_flag, days_quiet, display_stage, is_closed, quiet_label

router = APIRouter(prefix="/api")

_SORTS = {
    "last_signal": "a.last_signal_at desc nulls last",
    "stage_depth": "sd.depth desc nulls last",
    "company": "c.canonical_name asc",
}
_DEFAULT_SORT = "last_signal"
_MAX_LIMIT = 200

_ROWS = """
select a.id, c.canonical_name as company, a.role_title, a.current_stage, a.current_phase,
       a.status, a.applied_at, a.last_signal_at, a.confidence, a.needs_review,
       a.presumed_closed, s.channel,
       sd.stale_after_days,
       d.due_at as deadline_at, o.decide_by,
       p.p90_days
  from applications a
  join companies c on c.id = a.company_id
  left join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
  left join lateral (
    select channel from sources
     where application_id = a.id order by is_first_touch desc limit 1
  ) s on true
  left join lateral (
    select due_at from deadlines
     where application_id = a.id and met_at is null order by due_at limit 1
  ) d on true
  left join lateral (
    select decide_by from comp_offers
     where application_id = a.id and decide_by is not null order by created_at desc limit 1
  ) o on true
  left join lateral (
    select p90_days from stage_dwell_in
     where user_id = a.user_id and stage = a.current_stage and n >= 5
  ) p on true
 where a.user_id = $1 and a.merged_into_id is null
"""


@router.get("/applications")
async def list_applications(
    request: Request,
    phase: str | None = None,
    status: str | None = None,
    sort: str = _DEFAULT_SORT,
    limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    db = request.app.state.db

    conditions: list[str] = []
    params: list[object] = [session.user_id]
    # `phase=all` is how the client asks for no filter rather than for a phase
    # called "all".
    if phase and phase != "all":
        params.append(phase)
        conditions.append(f"and a.current_phase = ${len(params)}")
    if status and status != "all":
        params.append(status)
        conditions.append(f"and a.status = ${len(params)}")

    order = _SORTS.get(sort, _SORTS[_DEFAULT_SORT])
    params.append(limit)
    sql = f"{_ROWS} {' '.join(conditions)} order by {order} limit ${len(params)}"

    now = datetime.now(UTC)
    async with db.session(session.user_id) as connection:
        stages = await load_stage_table(connection, session.user_id)
        rows = await connection.fetch(sql, *params)
        tz = await connection.fetchval("select tz from users where id = $1", session.user_id)

    return {
        "rows": [_row(row, now=now, tz=tz or "UTC", stages=stages) for row in rows],
        # Never paginated today: the client asks for 200 and this mailbox has
        # dozens. Present and null so the shape does not change when it is.
        "next_cursor": None,
    }


def _row(row: Any, *, now: datetime, tz: str, stages: Any) -> dict[str, Any]:
    quiet = days_quiet(now, row["last_signal_at"])
    flag = compute_flag(
        now=now,
        tz=tz,
        status=row["status"],
        deadline_at=row["deadline_at"],
        decide_by=row["decide_by"],
        last_signal_at=row["last_signal_at"],
        quiet_threshold_days=_quiet_threshold(row),
    )
    return {
        "id": str(row["id"]),
        "company": row["company"],
        "role": row["role_title"],
        # The raw key and the label both travel: the client filters on one and
        # shows the other, and deriving either would put the stage machine in
        # the browser.
        "stage": row["current_stage"],
        "display_stage": display_stage(
            row["status"],
            row["current_stage"],
            stages,
            presumed_closed=bool(row["presumed_closed"]),
        ),
        "phase": row["current_phase"],
        "status": row["status"],
        "channel": row["channel"],
        "applied_at": iso_z(row["applied_at"]),
        "last_signal_at": iso_z(row["last_signal_at"]),
        "days_quiet": quiet,
        "quiet_label": quiet_label(quiet),
        "flag": flag.text,
        "flag_kind": flag.kind,
        "closed": is_closed(row["status"]),
        "needs_review": bool(row["needs_review"]),
        "confidence": num(row["confidence"]),
    }


def _quiet_threshold(row: Any) -> float | None:
    """Twice the user's own p90 for this stage, or the stage's staleness.

    The per-user figure only exists once there are enough observed transitions
    to mean anything; below that the stage's own default stands in.
    """
    p90 = row["p90_days"]
    if p90 is not None:
        return float(p90) * 2
    stale = row["stale_after_days"]
    return float(stale) if stale is not None else None
