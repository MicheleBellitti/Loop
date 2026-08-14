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

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel

from loop.chat.agent import (
    AgentEvent,
    ErrorEvent,
    FinalEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
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

    async def test_a_model_that_cannot_be_reached_is_an_error_event(self) -> None:
        model = ExplodingModel(turns=[])
        events = await _events(model, ())
        assert [type(e) for e in events] == [ErrorEvent]
        error = events[0]
        assert isinstance(error, ErrorEvent)
        assert error.code == "model_unreachable"


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
