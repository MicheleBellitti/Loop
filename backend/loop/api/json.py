"""Reading a request body, when several routes legitimately have none.

`POST /api/suggestions/:key/act` sends nothing at all, and the browser sends no
content type with an empty body — so a handler that insists on JSON rejects
those before it is reached. An absent body is an empty mapping here; a malformed
one is a 400.

**And a body has a ceiling.** The Fastify gateway this replaces was built with
`bodyLimit: 1_000_000` and nothing in the uvicorn/Starlette stack has an
equivalent, so `await request.body()` would buffer whatever arrived — on a box
the README sizes at under 700 MB idle, reachable without a session on
`/api/auth/recover` and `/api/auth/login/verify`. Every JSON body this API
accepts is a handful of fields; a megabyte is four orders of magnitude of room.

One route is not a handful of fields: an image the user attaches to a chat
message arrives base64-encoded, which is a third larger than the picture. It
asks for its own ceiling rather than raising this one, so the exception is
visible where it is taken and the default stays where every other route is.
"""

import json
from typing import Any, Final

from fastapi import Request

from .errors import ApiError

MAX_BODY_BYTES: Final = 1_000_000


async def read_json(request: Request, limit: int = MAX_BODY_BYTES) -> dict[str, Any]:
    raw = await _bounded_body(request, limit)
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


async def _bounded_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The body, or a 413 as soon as it is clear there will be too much of it.

    `Content-Length` is checked first because it costs nothing and stops the
    common case before a byte is read; the streaming check is what holds for a
    chunked request, which declares no length at all.
    """
    cached = getattr(request, "_body", None)
    if cached is not None:
        return bytes(cached)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "body_too_large", "request body is too large")

    chunks: list[bytes] = []
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise ApiError(413, "body_too_large", "request body is too large")
        chunks.append(chunk)

    # Where `Request.body()` caches, so a later `await request.body()` — or a
    # second `read_json` — reads what was already taken off the wire instead of
    # a stream somebody else consumed.
    body = b"".join(chunks)
    request._body = body
    return body
