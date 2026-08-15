"""Reading a request body, when several routes legitimately have none.

`POST /api/suggestions/:key/act` sends nothing at all, and the browser sends no
content type with an empty body — so a handler that insists on JSON rejects
those before it is reached. An absent body is an empty mapping here; a malformed
one is a 400.
"""

import json
from typing import Any

from fastapi import Request

from .errors import ApiError


async def read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise ApiError(400, "bad_body", "expected JSON") from error
    # A body that is a list or a string is not a body this API has a shape for,
    # and reading it as empty produces the same "you left something out" error
    # the handler would give anyway.
    return parsed if isinstance(parsed, dict) else {}
