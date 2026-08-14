"""The agent loop, against a scripted model and hand-made tools.

No server, no database: the model is a list of turns, a tool is a closure, and
what is under test is the control flow — which events come out, in what order,
what goes back into the next model call, and what the loop does when the model
misbehaves.
"""

import json
from collections.abc import AsyncIterator
from typing import Any, cast

from loop.chat.agent import (
    ErrorEvent,
    FinalEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    run_agent,
)
from loop.chat.llama import Completion, TokenDelta, ToolCall
from loop.chat.tools import Tool, ToolContext, ToolResult
from loop.db import Database


class ScriptedModel:
    """Each call plays the next turn; every request is kept for inspection."""

    def __init__(self, turns: list[list[TokenDelta | Completion]]) -> None:
        self._turns = turns
        self.requests: list[dict[str, Any]] = []

    async def stream_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> AsyncIterator[TokenDelta | Completion]:
        self.requests.append(
            {"model": model, "messages": list(messages), "tools": tools}
        )
        for item in self._turns.pop(0):
            yield item


class FailingModel:
    async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[TokenDelta | Completion]:
        raise RuntimeError("connection refused")
        yield TokenDelta("")  # pragma: no cover - makes this a generator


def _context() -> ToolContext:
    # The fakes below never touch the database or Google; the context only has
    # to exist.
    return ToolContext(db=cast(Database, None), user_id="user-1", google=None)


def _tool(record: list[dict[str, Any]], *, ok: bool = True) -> Tool:
    async def run(_context: ToolContext, args: dict[str, Any]) -> ToolResult:
        record.append(args)
        return ToolResult(ok=ok, payload={"rows": [1, 2]}, summary="2 rows")

    return Tool(
        name="lookup",
        description="a test tool",
        parameters={"type": "object", "properties": {}},
        run=run,
    )


def _answer(text: str) -> list[TokenDelta | Completion]:
    return [
        TokenDelta(text),
        Completion(content=text, tool_calls=(), finish_reason="stop"),
    ]


def _asks_for(name: str, arguments: str, call_id: str = "c1") -> list[TokenDelta | Completion]:
    return [
        Completion(
            content="",
            tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
            finish_reason="tool_calls",
        )
    ]


async def _events(model: Any, tools: list[Tool], **kwargs: Any) -> list[Any]:
    return [
        event
        async for event in run_agent(
            client=model,
            model="m",
            system="be helpful",
            history=[{"role": "user", "content": "come va la mia pipeline?"}],
            tools=tools,
            context=_context(),
            **kwargs,
        )
    ]


class TestTheLoop:
    async def test_an_answer_with_no_tools_ends_in_one_round(self) -> None:
        model = ScriptedModel([_answer("Tutto bene.")])
        events = await _events(model, [])

        assert [type(e) for e in events] == [TokenEvent, FinalEvent]
        final = events[-1]
        assert final.content == "Tutto bene."
        assert final.tool_trace == []
        # The system prompt leads and the history follows.
        first = model.requests[0]["messages"]
        assert first[0] == {"role": "system", "content": "be helpful"}
        assert first[1]["role"] == "user"

    async def test_a_tool_round_feeds_the_result_back_and_traces_it(self) -> None:
        called: list[dict[str, Any]] = []
        model = ScriptedModel(
            [_asks_for("lookup", '{"phase": "sent"}'), _answer("Ecco.")]
        )
        events = await _events(model, [_tool(called)])

        assert [type(e) for e in events] == [
            ToolStartEvent,
            ToolEndEvent,
            TokenEvent,
            FinalEvent,
        ]
        assert called == [{"phase": "sent"}]
        assert events[1].ok is True and events[1].summary == "2 rows"
        assert events[-1].tool_trace == [
            {
                "name": "lookup",
                "arguments": {"phase": "sent"},
                "ok": True,
                "summary": "2 rows",
            }
        ]

        # The second call carries the assistant's request and the tool's answer.
        second = model.requests[1]["messages"]
        assert second[-2]["role"] == "assistant"
        assert second[-2]["tool_calls"][0]["function"]["name"] == "lookup"
        tool_message = second[-1]
        assert tool_message["role"] == "tool"
        assert tool_message["tool_call_id"] == "c1"
        assert json.loads(tool_message["content"]) == {
            "ok": True,
            "result": {"rows": [1, 2]},
        }

    async def test_arguments_that_are_not_json_become_an_empty_mapping(self) -> None:
        called: list[dict[str, Any]] = []
        model = ScriptedModel([_asks_for("lookup", "{not json"), _answer("ok")])
        events = await _events(model, [_tool(called)])

        assert called == [{}]
        assert isinstance(events[-1], FinalEvent)

    async def test_an_unknown_tool_is_an_answer_not_a_crash(self) -> None:
        model = ScriptedModel([_asks_for("no_such_tool", "{}"), _answer("ok")])
        events = await _events(model, [])

        end = next(e for e in events if isinstance(e, ToolEndEvent))
        assert end.ok is False
        assert "no_such_tool" in end.summary

    async def test_a_tool_that_raises_reports_failure_and_the_loop_goes_on(self) -> None:
        async def explode(_context: ToolContext, _args: dict[str, Any]) -> ToolResult:
            raise RuntimeError("boom")

        tool = Tool(
            name="lookup",
            description="",
            parameters={"type": "object", "properties": {}},
            run=explode,
        )
        model = ScriptedModel([_asks_for("lookup", "{}"), _answer("pazienza")])
        events = await _events(model, [tool])

        end = next(e for e in events if isinstance(e, ToolEndEvent))
        assert end.ok is False
        assert isinstance(events[-1], FinalEvent)

    async def test_the_last_round_takes_the_tools_away(self) -> None:
        called: list[dict[str, Any]] = []
        model = ScriptedModel(
            [
                _asks_for("lookup", "{}", call_id="c1"),
                _asks_for("lookup", "{}", call_id="c2"),
                _answer("basta così"),
            ]
        )
        events = await _events(model, [_tool(called)], max_rounds=2)

        assert isinstance(events[-1], FinalEvent)
        assert model.requests[0]["tools"] is not None
        assert model.requests[1]["tools"] is not None
        # The third call is the budgetary full stop: no tools on offer.
        assert model.requests[2]["tools"] is None

    async def test_a_model_that_cannot_be_reached_is_an_error_event(self) -> None:
        events = await _events(FailingModel(), [])
        assert [type(e) for e in events] == [ErrorEvent]
        assert events[0].code == "model_unreachable"


class TestTheRegistry:
    def test_every_tool_wires_to_a_function_declaration(self) -> None:
        from loop.chat.tools import default_tools

        tools = default_tools()
        names = [tool.name for tool in tools]
        assert len(names) == len(set(names))
        assert "read_application_email" in names
        assert "start_backfill" in names
        for tool in tools:
            wired = tool.wire()
            assert wired["type"] == "function"
            assert wired["function"]["name"] == tool.name
            assert wired["function"]["parameters"]["type"] == "object"
