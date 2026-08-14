"""The loop: model, tools, model again, until there is an answer.

Deliberately not a framework. The whole of what an agent graph would buy here
is: call the model with the tools on offer; if it asked for calls, run them and
go round again; after a few rounds take the tools away so the last word is an
answer rather than another request. That is thirty lines of control flow, it is
exactly the shape a LangGraph would encode, and writing it out keeps every
decision — the round budget, what happens to a malformed call, what is
persisted — visible in one screen.

The model arrives through a protocol and the tools as values, so the loop runs
in a test against a scripted model with no server anywhere.
"""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from .llama import Completion, TokenDelta
from .tools import Tool, ToolContext, ToolResult

_log = logging.getLogger("loop.chat.agent")

# Enough for "look the application up, list its mail, read two messages" with
# room to spare; small enough that a model stuck in a loop is stopped while the
# user is still watching.
MAX_TOOL_ROUNDS: Final = 6

_MAX_ANSWER_TOKENS: Final = 1200
_TEMPERATURE: Final = 0.2

# What a tool result may weigh in the context. Payloads are for the model, but
# a model context is finite and one exuberant tool must not evict the
# conversation.
_MAX_RESULT_CHARS: Final = 24_000


class ChatModel(Protocol):
    """The one method the agent needs; `LlamaClient` provides it."""

    def stream_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> AsyncIterator[TokenDelta | Completion]: ...


@dataclass(frozen=True, slots=True)
class TokenEvent:
    text: str


@dataclass(frozen=True, slots=True)
class ToolStartEvent:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolEndEvent:
    call_id: str
    name: str
    ok: bool
    summary: str


@dataclass(frozen=True, slots=True)
class FinalEvent:
    content: str
    # One entry per tool call, summaries only — this is what `store` persists.
    tool_trace: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    code: str
    message: str


AgentEvent = TokenEvent | ToolStartEvent | ToolEndEvent | FinalEvent | ErrorEvent


async def run_agent(
    *,
    client: ChatModel,
    model: str,
    system: str,
    history: list[dict[str, Any]],
    tools: Sequence[Tool],
    context: ToolContext,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> AsyncIterator[AgentEvent]:
    """Stream one assistant turn.

    Yields tokens and tool events as they happen and exactly one terminal
    event: `FinalEvent` with the whole answer, or `ErrorEvent` if the model
    could not be reached at all. A failing *tool* is not an error here — its
    failure goes back to the model as a result, and the model explains it.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *history]
    by_name = {tool.name: tool for tool in tools}
    wire = [tool.wire() for tool in tools]
    trace: list[dict[str, Any]] = []
    spoken: list[str] = []

    for round_index in range(max_rounds + 1):
        # The last round runs without tools: whatever the model knows by now,
        # the turn ends in prose rather than in one more request.
        offered = wire if round_index < max_rounds else None
        completion: Completion | None = None
        try:
            async for item in client.stream_chat(
                model=model,
                messages=messages,
                tools=offered,
                temperature=_TEMPERATURE,
                max_tokens=_MAX_ANSWER_TOKENS,
            ):
                if isinstance(item, TokenDelta):
                    spoken.append(item.text)
                    yield TokenEvent(item.text)
                else:
                    completion = item
        except Exception as error:
            _log.warning("the model call failed: %s", error)
            yield ErrorEvent("model_unreachable", str(error))
            return

        if completion is None:
            yield ErrorEvent("model_unreachable", "the stream ended without completing")
            return

        # On the tool-less final round whatever came back is the answer, even
        # if the model hallucinated one more call into its text.
        if not completion.tool_calls or offered is None:
            yield FinalEvent(content="".join(spoken) or completion.content, tool_trace=trace)
            return

        # Thinking-out-loud before a tool call is kept: it is part of the
        # visible answer, and models narrate what they are about to look up.
        messages.append(_assistant_turn(completion))
        for call in completion.tool_calls:
            arguments = _parse_arguments(call.arguments)
            yield ToolStartEvent(call_id=call.id, name=call.name, arguments=arguments)
            result = await _run_tool(by_name.get(call.name), context, arguments, call.name)
            trace.append(
                {
                    "name": call.name,
                    "arguments": arguments,
                    "ok": result.ok,
                    "summary": result.summary,
                }
            )
            yield ToolEndEvent(
                call_id=call.id, name=call.name, ok=result.ok, summary=result.summary
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": _rendered(result),
                }
            )

    # Unreachable: the loop always returns on the tool-less final round.
    yield ErrorEvent("internal", "the agent loop ended without an answer")


def _assistant_turn(completion: Completion) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": completion.content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in completion.tool_calls
        ],
    }


def _parse_arguments(raw: str) -> dict[str, Any]:
    """The model wrote this, so it can be anything at all."""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _run_tool(
    tool: Tool | None, context: ToolContext, arguments: dict[str, Any], name: str
) -> ToolResult:
    """Never raises: whatever goes wrong becomes a result the model reads."""
    if tool is None:
        return ToolResult(
            ok=False,
            payload={"error": f"there is no tool called {name}"},
            summary=f"unknown tool {name}",
        )
    try:
        return await tool.run(context, arguments)
    except Exception:
        _log.exception("tool %s failed", name)
        return ToolResult(
            ok=False,
            payload={"error": "the tool failed; try something else"},
            summary=f"{name} failed",
        )


def _rendered(result: ToolResult) -> str:
    text = json.dumps({"ok": result.ok, "result": result.payload}, ensure_ascii=False)
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + " …(truncated)"
    return text
