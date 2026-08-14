"""The loop, run by LangGraph rather than written here.

`create_agent` already is the model → tools → model loop, maintained and
tested by people who run it against every provider quirk this module would
otherwise meet one production incident at a time (decisions.md LIB-1). What
stays ours is the translation at each edge: our tool registry in, our event
stream out — the five events the panel renders and the store persists.

The one policy the library expresses differently is the round budget: where
the hand-rolled loop withdrew the tools on a final round, LangGraph counts
supersteps and raises `GraphRecursionError` past the limit. That error is an
answer here, not a failure — the turn ends with whatever was said, plus the
trace of what was tried.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final, cast

import httpx
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from pydantic import SecretStr

from .llama import CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS
from .tools import Tool, ToolContext, langchain_tools

_log = logging.getLogger("loop.chat.agent")

# Enough for "look the application up, list its mail, read two messages" with
# room to spare; small enough that a model stuck in a loop is stopped while
# the user is still watching. One round is a model step plus a tool step.
MAX_TOOL_ROUNDS: Final = 6

_MAX_ANSWER_TOKENS: Final = 1200
_TEMPERATURE: Final = 0.2

_BUDGET_NOTE: Final = (
    "I ran out of tool budget before reaching an answer — ask again more "
    "narrowly and I will look at fewer things, more carefully."
)


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


def chat_model(*, base_url: str, model: str, api_key: str | None = None) -> BaseChatModel:
    """llama.cpp through langchain-openai — the same server rung 3 points at."""
    return ChatOpenAI(
        base_url=base_url,
        model=model,
        # llama.cpp ignores the key unless started with `--api-key`; the SDK
        # underneath insists on one either way.
        api_key=SecretStr(api_key or "not-needed"),
        temperature=_TEMPERATURE,
        max_completion_tokens=_MAX_ANSWER_TOKENS,
        timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        # The Responses API is OpenAI-only; llama.cpp speaks chat completions.
        use_responses_api=False,
    )


async def run_agent(
    *,
    model: BaseChatModel,
    system: str,
    history: list[dict[str, Any]],
    context: ToolContext,
    tools: tuple[Tool, ...] | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> AsyncIterator[AgentEvent]:
    """Stream one assistant turn.

    Yields tokens and tool events as they happen and exactly one terminal
    event: `FinalEvent` with the whole answer, or `ErrorEvent` if the model
    could not be reached at all. A failing *tool* is not an error here — its
    failure goes back to the model as a result, and the model explains it.
    `tools` overrides the default registry; the tests script it.
    """
    agent = create_agent(
        model=model, tools=langchain_tools(context, tools), system_prompt=system
    )
    # A round is one model superstep and one tool superstep, plus the closing
    # model call that answers.
    config: RunnableConfig = {"recursion_limit": 2 * max_rounds + 1}

    spoken: list[str] = []
    last_answer = ""
    trace: list[dict[str, Any]] = []

    try:
        async for event in agent.astream_events(
            cast(Any, {"messages": history}), version="v2", config=config
        ):
            kind = event["event"]
            data = cast(dict[str, Any], event.get("data") or {})
            if kind == "on_chat_model_stream":
                text = _text_of(data.get("chunk"))
                if text:
                    spoken.append(text)
                    yield TokenEvent(text)
            elif kind == "on_chat_model_end":
                # The fallback for a model that answered without streaming.
                last_answer = _text_of(data.get("output")) or last_answer
            elif kind == "on_tool_start":
                yield ToolStartEvent(
                    call_id=str(event.get("run_id") or ""),
                    name=str(event.get("name") or ""),
                    arguments=_arguments(data.get("input")),
                )
            elif kind == "on_tool_end":
                name = str(event.get("name") or "")
                ok, summary = _outcome(data.get("output"))
                trace.append(
                    {
                        "name": name,
                        "arguments": _arguments(data.get("input")),
                        "ok": ok,
                        "summary": summary,
                    }
                )
                yield ToolEndEvent(
                    call_id=str(event.get("run_id") or ""),
                    name=name,
                    ok=ok,
                    summary=summary,
                )
    except GraphRecursionError:
        yield FinalEvent(
            content="".join(spoken) or last_answer or _BUDGET_NOTE, tool_trace=trace
        )
        return
    except Exception as error:
        _log.warning("the model call failed: %s", error)
        yield ErrorEvent("model_unreachable", str(error))
        return

    yield FinalEvent(content="".join(spoken) or last_answer, tool_trace=trace)


def _text_of(message: Any) -> str:
    """The text of a chunk or message, whatever shape its content takes."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _arguments(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _outcome(output: Any) -> tuple[bool, str]:
    """`ok` and the one-line summary, lifted back out of the tool's answer.

    `rendered` in `tools.py` writes them into the ToolMessage precisely so
    this does not need a side channel around the graph. Anything else in the
    message — the library's own error text for a malformed call, say — is
    reported as a failure with its first line as the summary.
    """
    text = _text_of(output) if not isinstance(output, str) else output
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "ok" in parsed:
        return bool(parsed["ok"]), str(parsed.get("summary") or "")
    failed = getattr(output, "status", None) == "error"
    return (not failed and bool(text), text.strip().splitlines()[0][:120] if text else "")
