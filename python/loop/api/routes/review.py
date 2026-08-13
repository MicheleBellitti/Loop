"""The queue of things the ladder would not guess at.

Rung 4. Every item here is a message some rung read and refused to answer for,
carried up with its evidence attached — which is why `excerpt` exists at all and
is the single exception to "no table stores message text".

The response is the eight selected columns and nothing else: no label, no count,
no formatting. It is the one route in this API where nothing is computed, and
the four columns left out — `resolution`, `resolved_at`, `learned_pattern`,
`user_id` — are left out on purpose.
"""

from typing import Any

from fastapi import APIRouter, Request

from loop.api import auth
from loop.api.serialise import iso_z

router = APIRouter(prefix="/api")

_OPEN_ITEMS = """
select id, kind, evidence_ref, excerpt, candidates, application_id,
       created_at, expires_at
  from review_items
 where user_id = $1 and resolved_at is null
 order by created_at, id
"""
# The `, id` is one word more than the reference, which ordered on `created_at`
# alone — two items raised by the same message arrived in whatever order the
# plan produced. The ids are uuid v7, so this is the same order with ties broken.


@router.get("/review")
async def list_review_items(request: Request) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    async with request.app.state.db.session(session.user_id) as connection:
        rows = await connection.fetch(_OPEN_ITEMS, session.user_id)
    return {"items": [_item(row) for row in rows]}


def _item(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "evidence_ref": row["evidence_ref"],
        "excerpt": row["excerpt"],
        # Nested JSON, not a string: the client reads `candidates[].company` to
        # draw the choices.
        "candidates": row["candidates"] or [],
        "application_id": str(row["application_id"]) if row["application_id"] else None,
        "created_at": iso_z(row["created_at"]),
        "expires_at": iso_z(row["expires_at"]),
    }
