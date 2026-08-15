"""The queue of things the ladder would not guess at.

Rung 4. Every item here is a message some rung read and refused to answer for,
carried up with its evidence attached — which is why `excerpt` exists at all and
is the single exception to "no table stores message text".

The response is the eight selected columns and nothing else: no label, no count,
no formatting. It is the one route in this API where nothing is computed, and
the four columns left out — `resolution`, `resolved_at`, `learned_pattern`,
`user_id` — are left out on purpose.
"""

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from loop.api import auth
from loop.api.errors import ApiError
from loop.api.json import read_json
from loop.api.serialise import iso_z
from loop.db import Queue, publish
from loop.domain.messages import PendingEvent
from loop.domain.types import Rung
from loop.domain.wire import encode_pending_event

router = APIRouter(prefix="/api")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HUMAN_RUNG: Rung = 4

# What a human can answer. `intent` carries the corrected reading and `agree`
# says whether the ladder had it right, which is the labelled data §3.6 is about
# not throwing away.
_CHOICES = frozenset({"application", "new_application", "intent", "undo_merge"})

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
    async with request.app.state.db.session(session.user_id, read_only=True) as connection:
        rows = await connection.fetch(_OPEN_ITEMS, session.user_id)
    return {"items": [_item(row) for row in rows]}


@router.post("/review/{item_id}")
async def resolve(request: Request, item_id: str) -> dict[str, Any]:
    """Answer one question, and keep only the shape of the answer.

    `excerpt` is nulled unconditionally — the one place a message body was
    allowed to rest, deleted the moment it has served its purpose. What survives
    is `learned_pattern`, and only `{kind, answer}`: no company, no text, no
    application id. That is enough to write a better rule next month and not
    enough to reconstruct anything.
    """
    session = auth.require(getattr(request.state, "session", None))
    if not _UUID.match(item_id):
        raise ApiError(400, "bad_id", "that is not a review item id", "id")

    body = await read_json(request)
    choice = body.get("choice")
    if not isinstance(choice, dict) or choice.get("kind") not in _CHOICES:
        raise ApiError(400, "bad_body", "an answer", "choice")
    learn = body.get("learn", True) is not False

    async with request.app.state.db.session(session.user_id) as connection:
        item = await connection.fetchrow(
            """
            select kind, candidates from review_items
             where id = $1 and resolved_at is null
            """,
            item_id,
        )
        if item is None:
            raise ApiError(404, "not_found", "not found")

        if choice["kind"] == "undo_merge":
            await _split(connection, session.user_id, item)

        await connection.execute(
            """
            update review_items
               set resolved_at = now(), resolution = $2, excerpt = null,
                   learned_pattern = case when $3 then $4::jsonb else null end
             where id = $1
            """,
            item_id,
            choice,
            learn,
            {"kind": item["kind"], "answer": choice["kind"]},
        )
    return {"ok": True}


async def _split(connection: Any, user_id: str, item: Any) -> None:
    """Undoing a merge is a correction, so it goes through the log.

    The reference cleared `merged_into_id` here and published the event as
    well — two writers for one fact, and the one that needed a grant the
    gateway does not have. The event alone now carries it: the pipeline is the
    only thing that writes application state, and `field: merge` is a side
    effect it applies.
    """
    candidates = item["candidates"] or []
    merge = candidates[0] if candidates else None
    if not isinstance(merge, dict) or not merge.get("merged") or not merge.get("kept"):
        return
    await publish(
        connection,
        Queue.EVENT,
        encode_pending_event(
            PendingEvent(
                user_id=user_id,
                # Attached to the surviving application, while the row being
                # freed is named in the payload. It is one event about one
                # relationship, and the survivor is where its history lives.
                application_id=str(merge["kept"]),
                type="human_corrected",
                occurred_at=datetime.now(UTC),
                confidence=1.0,
                rung=_HUMAN_RUNG,
                payload={
                    "field": "merge",
                    "from": "merged",
                    "to": "split",
                    "merged_id": str(merge["merged"]),
                },
            )
        ),
    )


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
