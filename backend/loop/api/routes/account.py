"""Leaving.

Article 17 as a route rather than a support request. One call, one function, and
the queue is emptied before the rows are — because a deletion that leaves your
mail sitting in a queue waiting to be processed is not a deletion.

`erase_user` is `security definer` and it is the only thing in this system
allowed past the append-only trigger on the event log: it sets `loop.erasing`
for the length of its own transaction, which is a hatch named for its one
purpose rather than a general bypass.
"""

import base64
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Request

from loop.api import auth
from loop.api.errors import ApiError
from loop.api.json import read_json

router = APIRouter(prefix="/api")

_log = logging.getLogger("loop.api.account")

# Twelve characters of base64url. Enough to quote in a support thread, and
# deliberately stored nowhere.
_RECEIPT_BYTES = 9


@router.delete("/account")
async def erase(request: Request) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    body = await read_json(request)
    if body.get("confirm") != "DELETE":
        raise ApiError(400, "confirm_required", 'send {"confirm":"DELETE"}')

    async with request.app.state.db.session(session.user_id) as connection:
        await connection.execute("select erase_user($1)", session.user_id)

    receipt = base64.urlsafe_b64encode(secrets.token_bytes(_RECEIPT_BYTES)).decode()
    # The one record of the deletion, and it is a log line rather than a row —
    # a receipts table would be personal data surviving an erasure.
    _log.info("account deleted, receipt %s", receipt)
    return {"ok": True, "receipt": receipt}
