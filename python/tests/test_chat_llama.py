"""The llama.cpp wire client, against a transcript rather than a server.

httpx's MockTransport hands the client a canned response over the real
streaming interface, so what is under test is the part that has ever broken in
the wild: reading an SSE body whose chunks disagree about where a tool call's
name ends and its arguments begin.
"""

import json

import httpx
import pytest

from loop.chat.llama import Completion, LlamaClient, LlamaError, TokenDelta


def _sse(*chunks: object) -> bytes:
    lines = [f"data: {json.dumps(chunk)}" for chunk in chunks]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _delta(**delta: object) -> dict[str, object]:
    return {"choices": [{"delta": delta}]}


def _client(body: bytes, status: int = 200) -> LlamaClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                status, json={"data": [{"id": "qwen"}, {"id": "llava"}]}
            )
        return httpx.Response(
            status, content=body, headers={"content-type": "text/event-stream"}
        )

    return LlamaClient(
        base_url="http://llama.test/v1",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


async def _drain(client: LlamaClient) -> tuple[list[str], Completion]:
    tokens: list[str] = []
    completion: Completion | None = None
    async for item in client.stream_chat(model="m", messages=[]):
        if isinstance(item, TokenDelta):
            tokens.append(item.text)
        else:
            completion = item
    assert completion is not None
    return tokens, completion


class TestStreaming:
    async def test_tokens_arrive_in_order_and_join_into_the_content(self) -> None:
        client = _client(
            _sse(
                _delta(role="assistant"),
                _delta(content="Ciao"),
                _delta(content=", "),
                _delta(content="mondo"),
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            )
        )
        tokens, completion = await _drain(client)
        assert tokens == ["Ciao", ", ", "mondo"]
        assert completion.content == "Ciao, mondo"
        assert completion.finish_reason == "stop"
        assert completion.tool_calls == ()

    async def test_a_tool_call_fragmented_across_chunks_is_reassembled(self) -> None:
        client = _client(
            _sse(
                _delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "list_app", "arguments": ""},
                        }
                    ]
                ),
                _delta(
                    tool_calls=[
                        {"index": 0, "function": {"name": "lications", "arguments": '{"pha'}}
                    ]
                ),
                _delta(
                    tool_calls=[
                        {"index": 0, "function": {"arguments": 'se": "sent"}'}}
                    ]
                ),
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            )
        )
        _tokens, completion = await _drain(client)
        assert len(completion.tool_calls) == 1
        call = completion.tool_calls[0]
        assert call.id == "call_1"
        assert call.name == "list_applications"
        assert json.loads(call.arguments) == {"phase": "sent"}

    async def test_a_whole_call_in_one_chunk_reads_the_same(self) -> None:
        client = _client(
            _sse(
                _delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "abc",
                            "function": {"name": "get_statistics", "arguments": "{}"},
                        }
                    ]
                ),
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            )
        )
        _tokens, completion = await _drain(client)
        assert [c.name for c in completion.tool_calls] == ["get_statistics"]

    async def test_a_malformed_frame_is_skipped_rather_than_fatal(self) -> None:
        body = (
            b"data: this is not json\n\n"
            + _sse(_delta(content="ok"), {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        )
        client = _client(body)
        tokens, completion = await _drain(client)
        assert tokens == ["ok"]
        assert completion.content == "ok"

    async def test_a_refusal_is_an_error_with_the_status_in_it(self) -> None:
        client = _client(b"model is loading", status=503)
        with pytest.raises(LlamaError, match="503"):
            await _drain(client)


class TestModels:
    async def test_the_served_list_comes_back_as_ids(self) -> None:
        client = _client(b"")
        assert await client.models() == ["qwen", "llava"]
