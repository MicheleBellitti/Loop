"""The pipeline board, and one application's whole history.

Seventeen keys per row, all of them always present, and four of them computed
here rather than by the client: the stage label, how long it has been quiet, the
sentence describing that, and the flag. That is the rule the whole design turns
on — no statistic, no stage and no dormancy decision is derived client-side —
and it is what makes a second client cheap later.

The detail route adds `facts` and `events` to exactly those seventeen keys, so
the drawer and the row it opened from cannot disagree about anything.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from loop.api import auth, narrate
from loop.api.errors import ApiError
from loop.api.json import read_json
from loop.api.posting import BlockedUrl, Posting, fetch_posting, parse_posting
from loop.api.serialise import confidence, iso_z, num, quoted
from loop.db import Queue, load_stage_table, publish
from loop.domain import compute_flag, days_quiet, display_stage, is_closed, quiet_label
from loop.domain.messages import EventSource, PendingEvent
from loop.domain.types import Channel, Rung
from loop.domain.wire import encode_pending_event

_log = logging.getLogger("loop.api.applications")

# Rung 4 is the human, and a hand-written event says so rather than claiming a
# machine read it somewhere.
_HUMAN_RUNG: Rung = 4

router = APIRouter(prefix="/api")

# What the reference accepts as an application id, which is not what Python
# accepts. `uuid.UUID` takes braced and undashed forms and this does not, and a
# route that accepts more ids than the reference is a route that 404s where the
# reference 400s.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

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


_EVENTS = """
select id, type, occurred_at, to_stage, payload, confidence, rung, evidence_ref
  from application_events where application_id = $1
 order by occurred_at desc, id desc
"""

# Not the same row as `channel`, deliberately: this is the earliest source
# whatever it was, while `channel` is the one marked first touch. They can
# disagree, and the reference lets them.
_FIRST_SOURCE = """
select ats_vendor, posting_url from sources
 where application_id = $1 order by first_seen_at limit 1
"""

# `order by created_at` is one word more than the reference, which had no order
# at all — with two posted ranges on one application it showed whichever row
# came back first.
_COMP = """
select min_minor, max_minor, currency, kind from comp_offers
 where application_id = $1 order by created_at
"""


@router.get("/applications/{application_id}")
async def get_application(request: Request, application_id: str) -> dict[str, Any]:
    """One application: the board row, the facts under it, and the whole log."""
    session = auth.require(getattr(request.state, "session", None))
    if not _UUID.match(application_id):
        raise ApiError(400, "bad_id", "that is not an application id", "id")

    db = request.app.state.db
    now = datetime.now(UTC)
    async with db.session(session.user_id) as connection:
        stages = await load_stage_table(connection, session.user_id)
        tz = await connection.fetchval("select tz from users where id = $1", session.user_id)
        row = await connection.fetchrow(
            f"{_ROWS} and a.id = $2", session.user_id, application_id
        )
        if row is None:
            # A merged application is a 404 rather than a redirect to the row it
            # was merged into: the id the client held is no longer an
            # application, and saying so is more honest than quietly answering
            # about a different one.
            raise ApiError(404, "not_found", "no such application")
        events = await connection.fetch(_EVENTS, application_id)
        source = await connection.fetchrow(_FIRST_SOURCE, application_id)
        comp = await connection.fetch(_COMP, application_id)
        detail = await connection.fetchrow(
            "select location, work_mode from applications where id = $1", application_id
        )

    return {
        **_row(row, now=now, tz=tz or "UTC", stages=stages),
        "facts": _facts(row, source, comp, detail),
        "events": [_event(event, stages) for event in events],
    }


_CHANNELS: frozenset[Channel] = frozenset(
    {"linkedin", "indeed", "career_page", "referral", "recruiter", "other"}
)
_ARCHIVE_AS = frozenset({"dormant", "withdrawn"})
_MAX_BULK = 200


@dataclass(frozen=True, slots=True)
class _Manual:
    """The three fields quick add takes when there is no URL to read."""

    company: str
    role: str
    channel: Channel | None

# The fields a human is allowed to overrule, which is not every column. `merge`
# is written only by the review queue's undo, and `company_id` has no editor.
_CORRECTABLE = frozenset(
    {
        "stage",
        "status",
        "role_title",
        "seniority",
        "location",
        "work_mode",
        "channel",
        "applied_at",
        "comp_expectation",
    }
)


@router.post("/applications", status_code=201)
async def quick_add(request: Request) -> dict[str, Any]:
    """The one place the user, not the mailbox, is the source of truth.

    Two shapes: a posting URL, or the three fields by hand. The URL is read
    best-effort and never blocks the 201 — a posting behind a login is still an
    application you made, and refusing to record it because a page would not
    load would be the tail wagging the dog.

    The row is created here and then never moved from here: an `applied` event
    goes on the queue and the pipeline folds it, so `applied_at`, the channel
    and the confidence on the row a moment later are the log's answer rather
    than this handler's guess.
    """
    session = auth.require(getattr(request.state, "session", None))
    body = await read_json(request)
    posting_url = body.get("posting_url")
    manual = _manual(body)

    if not manual and not isinstance(posting_url, str):
        raise ApiError(400, "bad_body", "a posting url, or a company and a role")

    found = await _read_posting(request, posting_url) if posting_url else Posting()
    company = manual.company if manual else (found.company or "Unknown")
    role = (manual.role if manual else None) or found.role or "Unknown role"
    channel: Channel = (manual.channel if manual else None) or "career_page"
    applied_at = _moment(body.get("applied_at")) or datetime.now(UTC)

    db = request.app.state.db
    async with db.session(session.user_id) as connection:
        company_id = await connection.fetchval(
            """
            insert into companies (canonical_name) values ($1)
            on conflict (lower(canonical_name), coalesce(domain, '')) do update
              set canonical_name = excluded.canonical_name
            returning id
            """,
            company,
        )
        application_id = str(
            await connection.fetchval(
                """
                insert into applications
                  (user_id, company_id, role_title, current_stage, current_phase,
                   manually_created, confidence, location)
                values ($1,$2,$3,'applied','sent',true,1.0,$4)
                returning id
                """,
                session.user_id,
                company_id,
                role,
                found.location,
            )
        )
        await publish(
            connection,
            Queue.EVENT,
            encode_pending_event(
                PendingEvent(
                    user_id=session.user_id,
                    application_id=application_id,
                    type="applied",
                    occurred_at=applied_at,
                    confidence=1.0,
                    to_stage="applied",
                    rung=_HUMAN_RUNG,
                    payload={
                        "channel": channel,
                        "posting_url": posting_url or None,
                        "role_title": role,
                    },
                    source=EventSource(
                        channel=channel,
                        posting_url=posting_url or None,
                        ats_vendor=found.ats_vendor,
                        is_first_touch=True,
                    ),
                )
            ),
        )
        if found.comp:
            await connection.execute(
                """
                insert into comp_offers
                  (user_id, application_id, kind, min_minor, max_minor, currency)
                values ($1,$2,'posted_range',$3,$4,$5)
                """,
                session.user_id,
                application_id,
                found.comp["min_minor"],
                found.comp["max_minor"],
                found.comp["currency"].upper(),
            )

    return {"id": application_id, "company": company, "role": role, "channel": channel}


@router.post("/applications/{application_id}/archive")
async def archive_one(request: Request, application_id: str) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    body = await read_json(request)
    as_what = body.get("as")
    if as_what not in _ARCHIVE_AS:
        raise ApiError(400, "bad_body", "archive as dormant or withdrawn", "as")
    await _archive(request, session.user_id, [_an_id(application_id)], as_what)
    return {"ok": True}


@router.post("/applications/archive")
async def archive_many(request: Request) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    body = await read_json(request)
    ids = body.get("ids")
    # `as` defaults here and is required on the single-id route. Inherited, and
    # harmless: a bulk archive is the "these are over" gesture and dormant is
    # what that means.
    as_what = body.get("as", "dormant")
    if not isinstance(ids, list) or not 1 <= len(ids) <= _MAX_BULK:
        raise ApiError(400, "bad_body", f"between 1 and {_MAX_BULK} ids", "ids")
    if as_what not in _ARCHIVE_AS:
        raise ApiError(400, "bad_body", "archive as dormant or withdrawn", "as")

    await _archive(request, session.user_id, [_an_id(str(i)) for i in ids], as_what)
    # The requested count, not the affected one: nothing here checks that a row
    # exists, and saying "200" for 200 ids you do not own is what the reference
    # does.
    return {"ok": True, "count": len(ids)}


@router.post("/applications/{application_id}/correct")
async def correct(request: Request, application_id: str) -> dict[str, Any]:
    """A correction is a new event at confidence 1.0, never an edit.

    The log is append-only and the row is derived from it, so overruling the
    machine means adding to the record rather than overwriting it — which is
    also why a correction survives the next reprocess and a column write would
    not.
    """
    session = auth.require(getattr(request.state, "session", None))
    application_id = _an_id(application_id)
    body = await read_json(request)
    field = body.get("field")
    if field not in _CORRECTABLE:
        raise ApiError(400, "bad_body", "that field cannot be corrected", "field")
    if "to" not in body:
        raise ApiError(400, "bad_body", "a value to correct it to", "to")

    async with request.app.state.db.session(session.user_id) as connection:
        current = await connection.fetchrow(
            "select current_stage, status from applications where id = $1", application_id
        )
        if current is None:
            raise ApiError(404, "not_found", "no such application")
        # The reference read `status` for every field but `stage`, so correcting
        # a role title recorded `from: 'live'` and the drawer rendered
        # `role_title: live → Staff Engineer`. The before-value is now the field
        # being corrected, or absent when this handler cannot know it.
        before = _before(field, current)

        await publish(
            connection,
            Queue.EVENT,
            encode_pending_event(
                PendingEvent(
                    user_id=session.user_id,
                    application_id=application_id,
                    type="human_corrected",
                    occurred_at=datetime.now(UTC),
                    confidence=1.0,
                    rung=_HUMAN_RUNG,
                    payload={"field": field, "from": before, "to": body["to"]},
                )
            ),
        )
    return {"ok": True}


def _before(field: str, current: Any) -> Any:
    if field == "stage":
        return current["current_stage"]
    if field == "status":
        return current["status"]
    return None


async def _archive(
    request: Request, user_id: str, ids: list[str], as_what: str
) -> None:
    """One event per application, and no column written here.

    `status` and `current_stage` change when the pipeline folds the event, not
    before. The reference also stamped `last_user_action_at` inline; the
    projection recomputes that from the log seconds later, so the write bought
    nothing and cost the gateway an UPDATE grant on a table it should not be
    updating.
    """
    now = datetime.now(UTC)
    withdrawn = as_what == "withdrawn"
    async with request.app.state.db.session(user_id) as connection:
        for application_id in ids:
            await publish(
                connection,
                Queue.EVENT,
                encode_pending_event(
                    PendingEvent(
                        user_id=user_id,
                        application_id=application_id,
                        type="withdrawn" if withdrawn else "went_silent",
                        occurred_at=now,
                        confidence=1.0,
                        rung=_HUMAN_RUNG,
                        payload={} if withdrawn else {"threshold_used": "archived_by_user"},
                    )
                ),
            )


def _manual(body: dict[str, Any]) -> _Manual | None:
    """The by-hand shape, or None when this is a URL.

    A body carrying both is read as the URL, because that is the one that can
    be checked — and the reference resolves the union the same way.
    """
    if isinstance(body.get("posting_url"), str):
        return None
    company, role, channel = body.get("company"), body.get("role"), body.get("channel")
    if not (isinstance(company, str) and company and isinstance(role, str) and role):
        return None
    if channel is not None and channel not in _CHANNELS:
        raise ApiError(400, "bad_body", "not a channel", "channel")
    return _Manual(company=company, role=role, channel=channel)


async def _read_posting(request: Request, url: str) -> Posting:
    """Never fatal. A posting that cannot be read is a posting, not an error."""
    try:
        final_url, html = await fetch_posting(url)
    except (BlockedUrl, OSError) as error:
        _log.info("posting not read (%s): %s", url, error)
        return Posting()
    except Exception:
        _log.exception("posting not read (%s)", url)
        return Posting()
    return parse_posting(final_url, html)


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError(400, "bad_body", "not a timestamp", "applied_at") from error


def _an_id(value: str) -> str:
    if not _UUID.match(value):
        raise ApiError(400, "bad_id", "that is not an application id", "id")
    return value


def _facts(row: Any, source: Any, comp: Any, detail: Any) -> dict[str, Any]:
    offers = [_money(o) for o in comp if o["kind"] == "offer"]
    posted = next((_money(o) for o in comp if o["kind"] == "posted_range"), None)
    return {
        "applied": iso_z(row["applied_at"]),
        "ats": source["ats_vendor"] if source else None,
        "posting_url": source["posting_url"] if source else None,
        "location": _where(detail),
        "posted_range": posted,
        "offers": offers,
    }


def _where(detail: Any) -> str | None:
    """`Milano · remote`, or one of them, or nothing at all."""
    if detail is None:
        return None
    parts = [part for part in (detail["location"], detail["work_mode"]) if part]
    return " · ".join(parts) or None


def _money(row: Any) -> dict[str, Any]:
    # Quoted, because the reference leaves the cast off here and the client's
    # `money()` reads both. `/api/stats` casts the same column and sends a
    # number; both are reproduced rather than reconciled.
    return {
        "min_minor": quoted(row["min_minor"]),
        "max_minor": quoted(row["max_minor"]),
        "currency": row["currency"],
        "kind": row["kind"],
    }


def _event(event: Any, stages: Any) -> dict[str, Any]:
    payload = event["payload"] or {}
    return {
        # A bigserial, and a string on the wire — the same accident of a driver
        # that quotes the offer amounts above.
        "id": quoted(event["id"]),
        "when": iso_z(event["occurred_at"]),
        "what": narrate.title(event["type"], event["to_stage"], stages),
        "detail": narrate.detail(payload),
        "source": narrate.provenance(event["rung"], payload),
        # Two decimals as a string, while the application's own confidence on
        # the same response is a bare number. One numeric(3,2), two encodings.
        "conf": confidence(event["confidence"]),
        "rung": event["rung"],
        "evidence_ref": event["evidence_ref"],
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
