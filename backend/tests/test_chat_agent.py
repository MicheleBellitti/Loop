"""The agent loop — LangGraph underneath, our events on top.

The library's own control flow is its maintainers' problem to test
(decisions.md LIB-1). What is ours is the translation at each edge, and that
is what these tests pin: a scripted `BaseChatModel` goes in, and what must
come out is our event stream — tools as start/end pairs with the summary
lifted from the ToolMessage, tokens in order, one terminal event, and the
persistable trace with no payloads in it.
"""

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, cast

import httpx2
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel

from loop.chat.agent import (
    AgentEvent,
    ErrorEvent,
    FinalEvent,
    NoticeEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    chat_model,
    run_agent,
)
from loop.chat.tools import Tool, ToolContext, ToolResult
from loop.db import Database


class ScriptedModel(BaseChatModel):
    """Plays one AIMessage per call, streamed in pieces like a real engine.

    Past the end of the script it replays the last turn, which is what lets a
    test drive the loop into its recursion limit.
    """

    turns: list[AIMessage]
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _next(self) -> AIMessage:
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return turn

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        turn = self._next()
        if turn.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call["args"]),
                            "id": call["id"],
                            "index": index,
                            "type": "tool_call_chunk",
                        }
                        for index, call in enumerate(turn.tool_calls)
                    ],
                )
            )
            return
        text = str(turn.content)
        half = max(1, len(text) // 2)
        for piece in (text[:half], text[half:]):
            if piece:
                yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


class ExplodingModel(ScriptedModel):
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        raise RuntimeError("connection refused")

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise RuntimeError("connection refused")


class BlindModel(ScriptedModel):
    """A llama.cpp with no projector: refuses a picture, answers the words.

    The refusal is quoted from the real one, because recognising it is the
    whole of what the code under test does.
    """

    seen: list[list[BaseMessage]] = []

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self.seen.append(messages)
        if any("image_url" in str(message.content) for message in messages):
            raise RuntimeError(
                "Error code: 500 - {'error': {'code': 500, 'message': 'image input "
                "is not supported - hint: if this is unexpected, you may need to "
                "provide the mmproj', 'type': 'server_error'}}"
            )
        yield from super()._stream(messages, stop, run_manager, **kwargs)


class LookupArgs(BaseModel):
    phase: str | None = None


def _context() -> ToolContext:
    # The fakes below never touch the database or Google; the context only has
    # to exist.
    return ToolContext(db=cast(Database, None), user_id="user-1", google=None)


def _tool(record: list[dict[str, Any]], *, boom: bool = False) -> Tool:
    async def run(_context: ToolContext, args: dict[str, Any]) -> ToolResult:
        if boom:
            raise RuntimeError("boom")
        record.append(args)
        return ToolResult(ok=True, payload={"rows": [1, 2]}, summary="2 rows")

    return Tool(name="lookup", description="a test tool", args=LookupArgs, run=run)


def _asks_for_lookup(arguments: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": "lookup", "args": arguments, "id": "c1"}]
    )


async def _events(
    model: BaseChatModel, tools: tuple[Tool, ...], **kwargs: Any
) -> list[AgentEvent]:
    stream: AsyncIterator[AgentEvent] = run_agent(
        model=model,
        system="be helpful",
        history=[{"role": "user", "content": "come va la mia pipeline?"}],
        context=_context(),
        tools=tools,
        **kwargs,
    )
    return [event async for event in stream]


class TestTheLoop:
    async def test_an_answer_with_no_tools_streams_and_ends(self) -> None:
        model = ScriptedModel(turns=[AIMessage(content="Tutto bene.")])
        events = await _events(model, ())

        assert [type(e) for e in events] == [TokenEvent, TokenEvent, FinalEvent]
        final = events[-1]
        assert isinstance(final, FinalEvent)
        assert final.content == "Tutto bene."
        assert final.tool_trace == []

    async def test_a_tool_round_traces_and_feeds_back(self) -> None:
        called: list[dict[str, Any]] = []
        model = ScriptedModel(
            turns=[_asks_for_lookup({"phase": "sent"}), AIMessage(content="Ecco.")]
        )
        events = await _events(model, (_tool(called),))

        kinds = [type(e) for e in events]
        assert kinds == [ToolStartEvent, ToolEndEvent, TokenEvent, TokenEvent, FinalEvent]
        assert called == [{"phase": "sent"}]

        start = events[0]
        assert isinstance(start, ToolStartEvent)
        assert start.name == "lookup" and start.arguments == {"phase": "sent"}

        end = events[1]
        assert isinstance(end, ToolEndEvent)
        assert end.ok is True and end.summary == "2 rows"

        final = events[-1]
        assert isinstance(final, FinalEvent)
        assert final.content == "Ecco."
        # The persistable trace: name, arguments, outcome — and no payload.
        assert final.tool_trace == [
            {
                "name": "lookup",
                "arguments": {"phase": "sent"},
                "ok": True,
                "summary": "2 rows",
            }
        ]

    async def test_a_tool_that_raises_reports_failure_and_the_loop_goes_on(self) -> None:
        model = ScriptedModel(
            turns=[_asks_for_lookup({}), AIMessage(content="pazienza")]
        )
        events = await _events(model, (_tool([], boom=True),))

        end = next(e for e in events if isinstance(e, ToolEndEvent))
        assert end.ok is False
        assert "lookup failed" in end.summary
        final = events[-1]
        assert isinstance(final, FinalEvent)
        assert final.content == "pazienza"
        assert final.tool_trace[0]["ok"] is False

    async def test_a_model_stuck_on_tools_ends_in_an_answer_not_a_crash(self) -> None:
        called: list[dict[str, Any]] = []
        # The script never advances past the tool call, so the graph hits its
        # recursion limit — which must come out as a Final, budget named.
        model = ScriptedModel(turns=[_asks_for_lookup({})])
        events = await _events(model, (_tool(called),), max_rounds=2)

        final = events[-1]
        assert isinstance(final, FinalEvent)
        assert final.tool_trace
        assert final.content  # the budget note, or whatever was said

    async def test_a_model_that_cannot_see_drops_the_image_and_says_so(self) -> None:
        model = BlindModel(turns=[AIMessage(content="Dalla domanda direi…")])
        stream: AsyncIterator[AgentEvent] = run_agent(
            model=model,
            system="be helpful",
            history=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "che dice questo?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,aGk="},
                        },
                    ],
                }
            ],
            context=_context(),
            tools=(),
        )
        events = [event async for event in stream]

        assert [type(e) for e in events] == [
            NoticeEvent,
            TokenEvent,
            TokenEvent,
            FinalEvent,
        ]
        notice = events[0]
        assert isinstance(notice, NoticeEvent)
        assert notice.code == "no_vision"
        final = events[-1]
        assert isinstance(final, FinalEvent)
        assert final.content == "Dalla domanda direi…"

        # The retry kept the question and left the picture behind.
        asked = str(model.seen[-1][-1].content)
        assert "che dice questo?" in asked
        assert "image_url" not in asked

    async def test_a_model_that_cannot_be_reached_is_an_error_event(self) -> None:
        model = ExplodingModel(turns=[])
        events = await _events(model, ())
        assert [type(e) for e in events] == [ErrorEvent]
        error = events[0]
        assert isinstance(error, ErrorEvent)
        assert error.code == "model_unreachable"


class TestTheTransport:
    """One turn over the real client, which no other test here builds.

    Everything above scripts a `BaseChatModel` and never constructs
    `chat_model`, so the openai transport — the timeout object, the token cap's
    two spellings, the streaming parse — went untested in a default run, which
    is how a batch of defects in exactly those lines once shipped green. A mock
    transport needs no server and no database, so this stays in the pure suite.
    """

    @staticmethod
    def _sse(chunks: list[dict[str, Any]]) -> httpx2.Response:
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(body + "data: [DONE]\n\n").encode(),
        )

    @staticmethod
    def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "qwen",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    async def test_a_turn_streams_through_the_openai_client(self) -> None:
        sent: list[dict[str, Any]] = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            sent.append(json.loads(request.content))
            assert request.url.path == "/v1/chat/completions"
            return self._sse(
                [
                    self._chunk({"role": "assistant", "content": "Tutto "}),
                    self._chunk({"content": "bene."}),
                    self._chunk({}, finish="stop"),
                ]
            )

        model = chat_model(
            base_url="http://llama.test/v1",
            model="qwen",
            http=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        )
        stream: AsyncIterator[AgentEvent] = run_agent(
            model=model,
            system="be helpful",
            history=[{"role": "user", "content": "come va?"}],
            context=_context(),
            tools=(),
        )
        events = [event async for event in stream]

        assert [type(e) for e in events] == [TokenEvent, TokenEvent, FinalEvent]
        final = events[-1]
        assert isinstance(final, FinalEvent)
        assert final.content == "Tutto bene."

        body = sent[0]
        assert body["stream"] is True
        assert body["model"] == "qwen"
        # Both spellings of the cap, for llama.cpp builds either side of the
        # rename — and the system prompt where the server expects it.
        assert body["max_tokens"] == body["max_completion_tokens"]
        assert body["messages"][0] == {"role": "system", "content": "be helpful"}


class TestTheRegistry:
    def test_every_tool_binds_to_a_structured_tool(self) -> None:
        from loop.chat.tools import default_tools, langchain_tools

        specs = default_tools()
        names = [tool.name for tool in specs]
        assert len(names) == len(set(names))
        assert "read_application_email" in names
        assert "start_backfill" in names

        bound = langchain_tools(_context())
        assert [tool.name for tool in bound] == names
        for tool in bound:
            schema = tool.tool_call_schema.model_json_schema()  # type: ignore[union-attr]
            assert schema["type"] == "object"

    def test_rendered_carries_ok_and_summary_for_the_stream_layer(self) -> None:
        from loop.chat.tools import rendered

        text = rendered(ToolResult(ok=True, payload={"n": 1}, summary="one row"))
        parsed = json.loads(text)
        assert parsed == {"ok": True, "summary": "one row", "result": {"n": 1}}
