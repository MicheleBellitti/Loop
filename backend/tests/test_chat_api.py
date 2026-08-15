"""The chat surface, end to end: routes, store, agent, and a stub llama.cpp.

The stub speaks just enough of the OpenAI wire format to exercise the whole
loop — first call asks for a tool, second call answers — so what is under test
is everything of ours: the gate, the store, the agent, the SSE frames, and the
rule that what persists is a trace rather than a payload.
"""

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from loop.api import Settings, auth, create_app
from loop.db import Database

pytestmark = pytest.mark.integration


# ── a llama.cpp that always cooperates ──────────────────────────────────────


async def _read_body(receive: Any) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            return body


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    """One chat.completion.chunk, fully dressed — the SDK reads these."""
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _chunks_for(request: dict[str, Any]) -> list[dict[str, Any]]:
    asked_already = any(m.get("role") == "tool" for m in request["messages"])
    if asked_already:
        return [
            _chunk({"role": "assistant", "content": "La mailbox "}),
            _chunk({"content": "sta bene."}),
            _chunk({}, finish="stop"),
        ]
    return [
        _chunk(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "get_mailbox_health", "arguments": "{}"},
                    }
                ],
            }
        ),
        _chunk({}, finish="tool_calls"),
    ]


@dataclass
class Stub:
    """The stub server, and everything the gateway asked it for."""

    url: str
    seen: list[dict[str, Any]]

    @property
    def prompts(self) -> list[str]:
        """The system prompt of each completion request, in order."""
        return [
            next(
                (m["content"] for m in request["messages"] if m["role"] == "system"), ""
            )
            for request in self.seen
        ]


def _stub_llama(
    seen: list[dict[str, Any]], model_id: str = "stub-model", sees: bool = False
) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _serve(scope, receive, send, seen, model_id, sees)

    return app


async def _serve(
    scope: Any,
    receive: Any,
    send: Any,
    seen: list[dict[str, Any]],
    model_id: str,
    sees: bool,
) -> None:
    if scope["type"] != "http":
        return
    if scope["path"] == "/props":
        # llama.cpp's own endpoint. The default stub is a model with no
        # projector, which is the interesting case: it is the one that used to
        # come back to the user as a raw 500.
        payload = json.dumps({"modalities": {"vision": sees, "audio": False}}).encode()
        headers = [(b"content-type", b"application/json")]
    elif scope["path"].endswith("/models"):
        payload = json.dumps(
            {
                "object": "list",
                "data": [
                    {"id": model_id, "object": "model", "created": 0, "owned_by": "stub"}
                ],
            }
        ).encode()
        headers = [(b"content-type", b"application/json")]
    else:
        request = json.loads(await _read_body(receive))
        seen.append(request)
        frames = "".join(
            f"data: {json.dumps(chunk)}\n\n" for chunk in _chunks_for(request)
        )
        payload = (frames + "data: [DONE]\n\n").encode()
        headers = [(b"content-type", b"text/event-stream")]
    await send(
        {"type": "http.response.start", "status": 200, "headers": headers}
    )
    await send({"type": "http.response.body", "body": payload})


@contextlib.asynccontextmanager
async def _running(model_id: str, sees: bool = False) -> AsyncIterator[Stub]:
    import uvicorn

    seen: list[dict[str, Any]] = []
    config = uvicorn.Config(
        _stub_llama(seen, model_id, sees),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield Stub(url=f"http://127.0.0.1:{port}/v1", seen=seen)
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
async def llama_stub() -> AsyncIterator[Stub]:
    async with _running("stub-model") as stub:
        yield stub


@pytest.fixture
async def second_stub() -> AsyncIterator[Stub]:
    """A second server, with a second model — and one that can see."""
    async with _running("stub-vision", sees=True) as stub:
        yield stub


@pytest.fixture
async def chat_client(
    dsn: str, user_id: str, llama_stub: Stub
) -> AsyncIterator[AsyncClient]:
    """The API with a model configured, which the shared fixture leaves off."""
    app = create_app(
        Settings(dsn=dsn, session_secret="test-secret", model_base_url=llama_stub.url)
    )
    async with app.router.lifespan_context(app):
        token, _session = await app.state.sessions.create(user_id)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={auth.COOKIE_NAME: token},
        ) as http:
            yield http


@pytest.fixture
async def two_server_client(
    dsn: str, user_id: str, llama_stub: Stub, second_stub: Stub
) -> AsyncIterator[AsyncClient]:
    """The API pointed at both servers, which is what makes a picker a choice."""
    app = create_app(
        Settings(
            dsn=dsn,
            session_secret="test-secret",
            model_base_urls=(llama_stub.url, second_stub.url),
        )
    )
    async with app.router.lifespan_context(app):
        token, _session = await app.state.sessions.create(user_id)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={auth.COOKIE_NAME: token},
        ) as http:
            yield http


async def _csrf(client: AsyncClient) -> dict[str, str]:
    token = (await client.get("/api/me")).json()["csrf"]
    return {"x-csrf-token": token}


def _events_of(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE frames into (event, data) pairs, comments dropped.

    sse-starlette writes CRLF line endings; normalised here rather than
    handled twice below.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    for frame in body.replace("\r\n", "\n").split("\n\n"):
        kind, data = "", ""
        for line in frame.splitlines():
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if kind:
            found.append((kind, json.loads(data)))
    return found


# ── conversations ───────────────────────────────────────────────────────────


class TestConversations:
    async def test_created_listed_and_deleted(self, client: AsyncClient) -> None:
        headers = await _csrf(client)
        created = await client.post(
            "/api/chat/conversations", json={}, headers=headers
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        assert created.json()["title"] is None

        listed = (await client.get("/api/chat/conversations")).json()["conversations"]
        assert [c["id"] for c in listed] == [conversation_id]

        deleted = await client.delete(
            f"/api/chat/conversations/{conversation_id}", headers=headers
        )
        assert deleted.json() == {"ok": True}
        assert (await client.get("/api/chat/conversations")).json() == {
            "conversations": []
        }

    async def test_an_unknown_conversation_is_a_404(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/chat/conversations/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    async def test_a_malformed_id_is_a_400(self, client: AsyncClient) -> None:
        assert (await client.get("/api/chat/conversations/not-an-id")).status_code == 400


class TestModels:
    async def test_unconfigured_says_so_rather_than_failing(
        self, client: AsyncClient
    ) -> None:
        body = (await client.get("/api/chat/models")).json()
        assert body["configured"] is False
        assert body["models"] == []

    async def test_the_stub_list_arrives_with_the_default_first(
        self, chat_client: AsyncClient
    ) -> None:
        body = (await chat_client.get("/api/chat/models")).json()
        assert body["configured"] is True
        assert body["reachable"] is True
        assert body["models"][0] == body["default"]
        assert "stub-model" in body["models"]
        # This stub has no projector and says so, which is what lets the panel
        # grey out the attach button instead of failing on send.
        assert body["vision"] is False

    async def test_two_servers_make_two_models_to_choose_from(
        self, two_server_client: AsyncClient
    ) -> None:
        body = (await two_server_client.get("/api/chat/models")).json()
        assert {"stub-model", "stub-vision"} <= set(body["models"])
        # Which of them can see is per model, because it is per server.
        assert body["vision_by_model"] == {"stub-model": False, "stub-vision": True}

    async def test_the_turn_goes_to_the_server_that_serves_the_model(
        self, two_server_client: AsyncClient, llama_stub: Stub, second_stub: Stub
    ) -> None:
        headers = await _csrf(two_server_client)
        conversation_id = (
            await two_server_client.post(
                "/api/chat/conversations", json={}, headers=headers
            )
        ).json()["id"]
        response = await two_server_client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            json={"content": "ciao", "model": "stub-vision"},
            headers=headers,
        )
        assert response.status_code == 200
        _events_of(response.text)

        assert second_stub.seen, "the chosen model's server was never asked"
        assert not llama_stub.seen, "the turn went to the wrong server"
        assert second_stub.seen[0]["model"] == "stub-vision"

    async def test_sending_without_a_model_is_a_503(self, client: AsyncClient) -> None:
        headers = await _csrf(client)
        created = await client.post(
            "/api/chat/conversations", json={}, headers=headers
        )
        response = await client.post(
            f"/api/chat/conversations/{created.json()['id']}/messages",
            json={"content": "ciao"},
            headers=headers,
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "not_configured"


class TestAttachments:
    async def test_an_image_comes_back_as_it_went_in(
        self, client: AsyncClient
    ) -> None:
        headers = await _csrf(client)
        conversation_id = (
            await client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        pixels = b"\x89PNG\r\n\x1a\nfake-pixels"

        uploaded = await client.post(
            f"/api/chat/conversations/{conversation_id}/attachments",
            json={
                "media_type": "image/png",
                "data": base64.b64encode(pixels).decode(),
            },
            headers=headers,
        )
        assert uploaded.status_code == 201

        served = await client.get(f"/api/chat/attachments/{uploaded.json()['id']}")
        assert served.status_code == 200
        assert served.headers["content-type"] == "image/png"
        assert served.content == pixels

    async def test_the_size_cap_holds(self, client: AsyncClient) -> None:
        headers = await _csrf(client)
        conversation_id = (
            await client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        oversized = base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode()
        response = await client.post(
            f"/api/chat/conversations/{conversation_id}/attachments",
            json={"media_type": "image/png", "data": oversized},
            headers=headers,
        )
        assert response.status_code == 400

    async def test_an_image_under_the_cap_is_not_refused_by_the_transport(
        self, client: AsyncClient
    ) -> None:
        """The route's own ceiling, not the API's default megabyte.

        Base64 is a third larger than the picture, so the shared body limit
        would turn every screenshot over ~700 KB into a 413 — which is what
        the user would see, with nothing about images in it.
        """
        headers = await _csrf(client)
        conversation_id = (
            await client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        big = base64.b64encode(b"\x89PNG" + b"x" * (1_500_000 - 4)).decode()
        response = await client.post(
            f"/api/chat/conversations/{conversation_id}/attachments",
            json={"media_type": "image/png", "data": big},
            headers=headers,
        )
        assert response.status_code == 201

    async def test_only_images_are_accepted(self, client: AsyncClient) -> None:
        headers = await _csrf(client)
        conversation_id = (
            await client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        response = await client.post(
            f"/api/chat/conversations/{conversation_id}/attachments",
            json={"media_type": "application/pdf", "data": "aGk="},
            headers=headers,
        )
        assert response.status_code == 400
        assert response.json()["error"]["field"] == "media_type"


# ── the whole turn ──────────────────────────────────────────────────────────


class TestATurn:
    async def test_the_stream_carries_tools_tokens_and_the_answer(
        self, chat_client: AsyncClient, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        headers = await _csrf(chat_client)
        conversation_id = (
            await chat_client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]

        response = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            json={"content": "come sta la mailbox?"},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"

        events = _events_of(response.text)
        kinds = [kind for kind, _data in events]
        assert kinds == ["tool.start", "tool.end", "token", "token", "done"]

        start = events[0][1]
        assert start["name"] == "get_mailbox_health"
        end = events[1][1]
        assert end["ok"] is True

        done = events[-1][1]
        assert done["content"] == "La mailbox sta bene."
        assert done["tool_trace"][0]["name"] == "get_mailbox_health"

        # What persisted: both turns, the trace on the assistant's, no payload.
        stored = (
            await chat_client.get(f"/api/chat/conversations/{conversation_id}")
        ).json()["messages"]
        assert [m["role"] for m in stored] == ["user", "assistant"]
        assert stored[1]["content"] == "La mailbox sta bene."
        assert stored[1]["tool_trace"][0]["summary"]
        assert "payload" not in stored[1]["tool_trace"][0]
        assert stored[1]["model"] == "qwen2.5-7b-instruct"

        # The first user message became the title.
        listed = (
            await chat_client.get("/api/chat/conversations")
        ).json()["conversations"]
        assert listed[0]["title"] == "come sta la mailbox?"
        assert listed[0]["message_count"] == 2

    async def test_an_attachment_travels_with_the_message(
        self, chat_client: AsyncClient
    ) -> None:
        headers = await _csrf(chat_client)
        conversation_id = (
            await chat_client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        uploaded = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/attachments",
            json={"media_type": "image/png", "data": base64.b64encode(b"img").decode()},
            headers=headers,
        )
        attachment_id = uploaded.json()["id"]

        response = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            json={"content": "guarda questo", "attachment_ids": [attachment_id]},
            headers=headers,
        )
        assert response.status_code == 200

        stored = (
            await chat_client.get(f"/api/chat/conversations/{conversation_id}")
        ).json()["messages"]
        assert stored[0]["attachment_ids"] == [attachment_id]

    async def test_the_second_turn_carries_the_first(
        self, chat_client: AsyncClient, llama_stub: Stub
    ) -> None:
        """Memory, which is the conversation being sent, not the model having one."""
        headers = await _csrf(chat_client)
        conversation_id = (
            await chat_client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]

        for said in ("mi chiamo Michele", "come mi chiamo?"):
            response = await chat_client.post(
                f"/api/chat/conversations/{conversation_id}/messages",
                json={"content": said},
                headers=headers,
            )
            assert response.status_code == 200
            _events_of(response.text)  # drain it, or the next turn races the write

        # Whatever the model does with them, both turns and the answer between
        # them were in front of it.
        last = llama_stub.seen[-1]["messages"]
        # The tool-calling turn contributes an assistant message with no text;
        # what matters is the conversation that is there, and in order.
        said = [
            m["content"]
            for m in last
            if m["role"] in {"user", "assistant"} and m["content"]
        ]
        assert said == ["mi chiamo Michele", "La mailbox sta bene.", "come mi chiamo?"]

    async def test_the_open_application_is_named_in_the_prompt(
        self, chat_client: AsyncClient, llama_stub: Stub, db: Database, user_id: str
    ) -> None:
        """What the panel is looking at, so "this one" resolves to something."""
        async with db.session(user_id) as connection:
            company = await connection.fetchval(
                """
                insert into companies (canonical_name, domain)
                values ('Prima','prima.it')
                on conflict (lower(canonical_name), coalesce(domain, '')) do update
                  set canonical_name = excluded.canonical_name
                returning id
                """
            )
            application_id = str(
                await connection.fetchval(
                    """
                    insert into applications
                      (user_id, company_id, role_title, current_stage, current_phase,
                       confidence, last_signal_at)
                    values ($1,$2,'Staff Engineer','applied','sent',1.0,$3)
                    returning id
                    """,
                    user_id,
                    company,
                    datetime.now(UTC),
                )
            )

        headers = await _csrf(chat_client)
        conversation_id = (
            await chat_client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        response = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            json={"content": "perché questa è ferma?", "application_id": application_id},
            headers=headers,
        )
        assert response.status_code == 200
        _events_of(response.text)

        prompt = llama_stub.prompts[-1]
        assert "Prima · Staff Engineer" in prompt
        assert application_id in prompt

    async def test_an_application_that_is_not_this_users_is_ignored(
        self, chat_client: AsyncClient, llama_stub: Stub
    ) -> None:
        headers = await _csrf(chat_client)
        conversation_id = (
            await chat_client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        response = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            json={
                "content": "e questa?",
                "application_id": "01890000-0000-7000-8000-000000000999",
            },
            headers=headers,
        )
        assert response.status_code == 200
        _events_of(response.text)
        assert "currently has the application" not in llama_stub.prompts[-1]

    async def test_a_retry_replaces_the_answer_rather_than_asking_twice(
        self, chat_client: AsyncClient
    ) -> None:
        headers = await _csrf(chat_client)
        conversation_id = (
            await chat_client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        first = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            json={"content": "come sta la mailbox?"},
            headers=headers,
        )
        _events_of(first.text)

        again = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/retry",
            json={},
            headers=headers,
        )
        assert again.status_code == 200
        kinds = [kind for kind, _data in _events_of(again.text)]
        assert kinds[-1] == "done"

        # Still one exchange: the question was not asked twice, and the answer
        # on record is the new one.
        stored = (
            await chat_client.get(f"/api/chat/conversations/{conversation_id}")
        ).json()["messages"]
        assert [m["role"] for m in stored] == ["user", "assistant"]
        assert stored[0]["content"] == "come sta la mailbox?"
        assert stored[1]["content"] == "La mailbox sta bene."

    async def test_a_retry_with_nothing_to_replace_is_refused(
        self, chat_client: AsyncClient
    ) -> None:
        headers = await _csrf(chat_client)
        conversation_id = (
            await chat_client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        response = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/retry", json={}, headers=headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "nothing_to_retry"

    async def test_an_empty_message_is_refused(self, chat_client: AsyncClient) -> None:
        headers = await _csrf(chat_client)
        conversation_id = (
            await chat_client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        response = await chat_client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            json={"content": "   "},
            headers=headers,
        )
        assert response.status_code == 400
