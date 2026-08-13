"""Everything, in a file, whenever you ask.

Article 20 as a route rather than a support request, and the counterweight to a
product that reads your mailbox: the way out is a download, not a conversation.
No rate limit, no pagination, no cursor — it is the owner's own data and there
is nobody to protect it from.

Three departures from the reference, all in the same direction.

It exports the whole account. The reference dumped five tables and left out
deadlines, review items, suggestions and the stage definitions — which meant the
exported `current_stage` keys had no labels to resolve against, and a data
subject asking for their data got about half of it.

It does not export `role_embedding`. A `select a.*` swept up a 384-float vector
per application: a few kilobytes each of internal ML artefact, in a file a
person opens.

And `decide_by` goes out as the date it is. It is a `date` column, and the
reference's driver parsed it to local midnight and then serialised it as UTC, so
a deadline of 2026-03-01 left the building as `2026-02-28T23:00:00.000Z` — off
by a day, and off by a different day depending on the server's timezone.
"""

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from loop.api import auth
from loop.api.serialise import iso_z

router = APIRouter(prefix="/api")

# Never exported. Not secret, just not anybody's data — an internal
# representation that would only make the file harder to read.
_INTERNAL = frozenset({"role_embedding"})

_TABLES = {
    "applications": """
        select a.*, c.canonical_name as company from applications a
          join companies c on c.id = a.company_id
         where a.user_id = $1 order by a.created_at, a.id
    """,
    "events": """
        select * from application_events where user_id = $1 order by occurred_at, id
    """,
    "sources": "select * from sources where user_id = $1 order by first_seen_at, id",
    "interviews": "select * from interviews where user_id = $1 order by starts_at, id",
    "comp_offers": "select * from comp_offers where user_id = $1 order by created_at, id",
    "deadlines": "select * from deadlines where user_id = $1 order by due_at, id",
    "review_items": "select * from review_items where user_id = $1 order by created_at, id",
    "suggestions": "select * from suggestions where user_id = $1 order by created_at, id",
    "stage_defs": "select * from stage_defs where user_id = $1 order by depth",
}


@router.get("/export")
async def export(request: Request, format: str = "json") -> Response:
    session = auth.require(getattr(request.state, "session", None))

    async with request.app.state.db.session(session.user_id) as connection:
        if format == "csv":
            # Only the applications, and only that query: the reference ran all
            # five and discarded four.
            rows = await connection.fetch(_TABLES["applications"], session.user_id)
            return _csv([_plain(row) for row in rows])
        tables = {
            name: [_plain(row) for row in await connection.fetch(sql, session.user_id)]
            for name, sql in _TABLES.items()
        }

    return JSONResponse(
        tables,
        headers={"content-disposition": 'attachment; filename="loop-export.json"'},
    )


def _csv(rows: list[dict[str, Any]]) -> Response:
    """A spreadsheet's view: one table, one row each, headers always.

    An empty account exports the header row rather than an empty file, so what
    opens in a spreadsheet is a table with no rows rather than a document with
    no columns.
    """
    columns = list(rows[0]) if rows else [c for c in _CSV_COLUMNS if c not in _INTERNAL]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": 'attachment; filename="loop-applications.csv"'},
    )


# The header an empty export still carries. Written out rather than derived
# because there is no row to derive it from, which is the whole case.
_CSV_COLUMNS = (
    "id",
    "company",
    "role_title",
    "current_stage",
    "current_phase",
    "status",
    "applied_at",
    "last_signal_at",
    "confidence",
)


def _plain(row: Any) -> dict[str, Any]:
    """A row as JSON, with the two things a driver would get wrong.

    Timestamps in the same shape as every other timestamp this API writes, and
    a `date` left as a date — the column says a day, and turning it into an
    instant is what moves it.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in _INTERNAL:
            continue
        if isinstance(value, datetime):
            out[key] = iso_z(value)
        elif isinstance(value, date):
            out[key] = value.isoformat()
        elif isinstance(value, UUID):
            out[key] = str(value)
        elif isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, bytes):
            out[key] = value.hex()
        else:
            out[key] = value
    return out
