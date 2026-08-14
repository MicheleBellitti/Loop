"""The assistant's HTTP surface.

The one route that is unlike the rest of this API is the message post: it
answers with a server-sent event stream rather than a document, because the
model produces tokens and tool calls over seconds and a panel that waited for
the whole answer would feel broken. `EventSource` cannot POST, so the client
reads the response body with `fetch` — same frames, different reader.

What streams and what persists are different things on purpose. Tokens and
tool events stream; what lands in `chat_messages` is the finished answer and a
tool *trace* — name, arguments, one-line outcome. A tool's payload can carry
email text, and email text does not touch a table (§04).
"""

import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import Response
from langchain_core.language_models.chat_models import BaseChatModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from loop.api import auth
from loop.api.errors import ApiError
from loop.api.json import read_json
from loop.chat import store
from loop.chat.agent import (
    ErrorEvent,
    FinalEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    chat_model,
    run_agent,
)
from loop.chat.llama import LlamaClient, LlamaError
from loop.chat.prompt import SYSTEM_PROMPT
from loop.chat.tools import ToolContext
from loop.google.client import GoogleClient

_log = logging.getLogger("loop.api.chat")

router = APIRouter(prefix="/api")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# The same posture as `/api/stream`: no buffering anywhere between the model
# and the panel. sse-starlette owns the rest of the stream headers, and its
# periodic ping owns the keep-alive — but not `no-transform`, which is the
# half of this that stops an intermediary recompressing the stream into one
# buffered lump, so it is stated here as the sibling route states it.
_STREAM_HEADERS: Final = {
    "cache-control": "no-cache, no-transform",
    "x-accel-buffering": "no",
}

_IMAGE_TYPES: Final = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)
# Generous for a screenshot, small enough that the table stays a table.
_MAX_IMAGE_BYTES: Final = 2 * 1024 * 1024
_MAX_PENDING_ATTACHMENTS: Final = 8
_MAX_ATTACHMENTS_PER_MESSAGE: Final = 4
_MAX_CONTENT_CHARS: Final = 8000


@router.get("/chat/models")
async def models(request: Request) -> dict[str, Any]:
    """What there is to pick from: the server's list, or the configured name.

    `configured` false means the whole feature is off — same switch as rung 3,
    deliberately: one `MODEL_BASE_URL` decides whether a model runs anywhere.
    """
    auth.require(getattr(request.state, "session", None))
    settings = request.app.state.settings
    if not settings.model_base_url:
        return {
            "configured": False,
            "reachable": False,
            "default": settings.model_name,
            "models": [],
        }

    client = LlamaClient(settings.model_base_url, api_key=settings.model_api_key)
    try:
        served = await client.models()
        reachable = True
    except LlamaError:
        served = []
        reachable = False
    finally:
        await client.aclose()

    if settings.model_name not in served:
        served = [settings.model_name, *served]
    return {
        "configured": True,
        "reachable": reachable,
        "default": settings.model_name,
        "models": served,
    }


@router.get("/chat/conversations")
async def conversations(request: Request) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    return {
        "conversations": await store.list_conversations(
            request.app.state.db, session.user_id
        )
    }


@router.post("/chat/conversations", status_code=201)
async def create(request: Request) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    body = await read_json(request)
    model = body.get("model")
    return await store.create_conversation(
        request.app.state.db,
        session.user_id,
        model=model if isinstance(model, str) and model else None,
    )


@router.get("/chat/conversations/{conversation_id}")
async def messages(request: Request, conversation_id: str) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    db = request.app.state.db
    await _known(db, session.user_id, _an_id(conversation_id))
    return {
        "messages": await store.messages_of(db, session.user_id, conversation_id)
    }


@router.delete("/chat/conversations/{conversation_id}")
async def delete(request: Request, conversation_id: str) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    deleted = await store.delete_conversation(
        request.app.state.db, session.user_id, _an_id(conversation_id)
    )
    if not deleted:
        raise ApiError(404, "not_found", "no such conversation")
    return {"ok": True}


@router.post("/chat/conversations/{conversation_id}/attachments", status_code=201)
async def attach(request: Request, conversation_id: str) -> dict[str, Any]:
    """One image, as JSON with the bytes in base64.

    JSON rather than multipart on purpose: the whole API reads one body shape,
    and multipart would be this route's private dependency for no gain a
    2 MB cap does not already provide.
    """
    session = auth.require(getattr(request.state, "session", None))
    db = request.app.state.db
    await _known(db, session.user_id, _an_id(conversation_id))

    body = await read_json(request)
    media_type = body.get("media_type")
    if media_type not in _IMAGE_TYPES:
        raise ApiError(400, "bad_body", "png, jpeg, webp or gif", "media_type")
    data = body.get("data")
    if not isinstance(data, str) or not data:
        raise ApiError(400, "bad_body", "the image bytes, base64-encoded", "data")
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError as error:
        raise ApiError(400, "bad_body", "that is not base64", "data") from error
    if not decoded or len(decoded) > _MAX_IMAGE_BYTES:
        raise ApiError(400, "bad_body", "images are capped at 2 MB", "data")

    pending = await store.unbound_attachments(db, session.user_id, conversation_id)
    if pending >= _MAX_PENDING_ATTACHMENTS:
        raise ApiError(400, "too_many", "send a message before attaching more")

    attachment_id = await store.add_attachment(
        db, session.user_id, conversation_id, media_type=media_type, data=decoded
    )
    return {"id": attachment_id}


@router.get("/chat/attachments/{attachment_id}")
async def serve_attachment(request: Request, attachment_id: str) -> Response:
    session = auth.require(getattr(request.state, "session", None))
    found = await store.attachment(
        request.app.state.db, session.user_id, _an_id(attachment_id)
    )
    if found is None:
        raise ApiError(404, "not_found", "no such attachment")
    media_type, data = found
    # Immutable by construction — an attachment row is never updated — so the
    # browser may keep it for as long as it keeps the conversation open.
    return Response(
        content=data,
        media_type=media_type,
        headers={"cache-control": "private, max-age=31536000, immutable"},
    )


@router.post("/chat/conversations/{conversation_id}/messages")
async def send(request: Request, conversation_id: str) -> EventSourceResponse:
    """One user turn in, one assistant turn out, streamed as it happens."""
    session = auth.require(getattr(request.state, "session", None))
    settings = request.app.state.settings
    db = request.app.state.db
    await _known(db, session.user_id, _an_id(conversation_id))

    if not settings.model_base_url:
        raise ApiError(
            503, "not_configured", "no model is configured; set MODEL_BASE_URL"
        )

    body = await read_json(request)
    content = body.get("content")
    if not isinstance(content, str):
        content = ""
    content = content.strip()[:_MAX_CONTENT_CHARS]
    attachment_ids = _attachment_ids(body)
    if not content and not attachment_ids:
        raise ApiError(400, "bad_body", "say something, or attach something", "content")

    model = await _resolve_model(request, session.user_id, conversation_id, body)

    await store.append_message(
        db,
        session.user_id,
        conversation_id,
        role="user",
        content=content,
        attachment_ids=attachment_ids,
    )
    history = await store.model_history(db, session.user_id, conversation_id)

    return EventSourceResponse(
        _frames(request, session.user_id, conversation_id, model, history),
        headers=_STREAM_HEADERS,
    )


def _cached_chat_model(request: Request, model: str) -> BaseChatModel:
    """One model per served model name, kept on the app.

    `ChatOpenAI` owns a connection pool and offers nothing to close it with,
    so building one per message leaks a pool per message — and the timeout it
    holds is unhashable, so langchain's own client cache never catches the
    duplicate either. Nothing about it is per-turn: the conversation lives in
    the history handed to `run_agent`, not in the client.
    """
    settings = request.app.state.settings
    cache: dict[tuple[str | None, str], BaseChatModel] | None
    cache = getattr(request.app.state, "chat_models", None)
    if cache is None:
        cache = {}
        request.app.state.chat_models = cache
    key = (settings.model_base_url, model)
    if key not in cache:
        cache[key] = chat_model(
            base_url=settings.model_base_url,
            model=model,
            api_key=settings.model_api_key,
        )
    return cache[key]


async def _frames(
    request: Request,
    user_id: str,
    conversation_id: str,
    model: str,
    history: list[dict[str, Any]],
) -> AsyncIterator[ServerSentEvent]:
    """The agent's events, as SSE frames, with the clients cleaned up after.

    Everything in here must catch its own failures: by the time this runs the
    200 has already gone out, so an exception can only reach the user as an
    `error` frame — never as a status code.
    """
    settings = request.app.state.settings
    google = (
        GoogleClient(
            client_id=settings.google.client_id or "",
            client_secret=settings.google.client_secret or "",
        )
        if settings.google.configured
        else None
    )
    context = ToolContext(db=request.app.state.db, user_id=user_id, google=google)

    yield ServerSentEvent(comment="thinking")
    try:
        async for event in run_agent(
            model=_cached_chat_model(request, model),
            system=SYSTEM_PROMPT,
            history=history,
            context=context,
        ):
            if isinstance(event, TokenEvent):
                yield _frame("token", {"text": event.text})
            elif isinstance(event, ToolStartEvent):
                yield _frame(
                    "tool.start",
                    {
                        "call_id": event.call_id,
                        "name": event.name,
                        "arguments": event.arguments,
                    },
                )
            elif isinstance(event, ToolEndEvent):
                yield _frame(
                    "tool.end",
                    {
                        "call_id": event.call_id,
                        "name": event.name,
                        "ok": event.ok,
                        "summary": event.summary,
                    },
                )
            elif isinstance(event, FinalEvent):
                message_id = await store.append_message(
                    request.app.state.db,
                    user_id,
                    conversation_id,
                    role="assistant",
                    content=event.content,
                    tool_trace=event.tool_trace,
                    model=model,
                )
                yield _frame(
                    "done",
                    {
                        "message_id": message_id,
                        "content": event.content,
                        "tool_trace": event.tool_trace,
                    },
                )
            elif isinstance(event, ErrorEvent):
                yield _frame("error", {"code": event.code, "message": event.message})
    except Exception:
        _log.exception("the chat stream failed")
        yield _frame("error", {"code": "internal", "message": "something failed"})
    finally:
        if google is not None:
            await google.aclose()


def _frame(kind: str, payload: dict[str, Any]) -> ServerSentEvent:
    return ServerSentEvent(event=kind, data=json.dumps(payload, ensure_ascii=False))


async def _resolve_model(
    request: Request, user_id: str, conversation_id: str, body: dict[str, Any]
) -> str:
    """The message's model, the conversation's, or the configured default.

    A choice made on a message sticks to the conversation, so the picker shows
    where the thread actually runs rather than where it started.
    """
    settings = request.app.state.settings
    chosen = body.get("model")
    if isinstance(chosen, str) and chosen.strip():
        model = chosen.strip()[:200]
        await store.set_model(request.app.state.db, user_id, conversation_id, model)
        return model
    async with request.app.state.db.session(user_id) as connection:
        stored = await connection.fetchval(
            "select model from chat_conversations where id = $1 and user_id = $2",
            conversation_id,
            user_id,
        )
    return str(stored) if stored else str(settings.model_name)


def _attachment_ids(body: dict[str, Any]) -> list[str]:
    raw = body.get("attachment_ids")
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > _MAX_ATTACHMENTS_PER_MESSAGE:
        raise ApiError(
            400,
            "bad_body",
            f"up to {_MAX_ATTACHMENTS_PER_MESSAGE} attachments",
            "attachment_ids",
        )
    ids: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not _UUID.match(value):
            raise ApiError(400, "bad_body", "that is not an attachment id", "attachment_ids")
        ids.append(value)
    return ids


async def _known(db: Any, user_id: str, conversation_id: str) -> None:
    if not await store.conversation_exists(db, user_id, conversation_id):
        raise ApiError(404, "not_found", "no such conversation")


def _an_id(value: str) -> str:
    if not _UUID.match(value):
        raise ApiError(400, "bad_id", "that is not an id", "id")
    return value
