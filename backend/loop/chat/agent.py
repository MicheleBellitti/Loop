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

import httpx2
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

_NO_VISION_NOTE: Final = (
    "This model cannot see images, so the attachment was left out and the "
    "question answered from its text alone. Start llama.cpp with --mmproj, or "
    "pick a model that reads images, to have it looked at."
)

# What a server says when it was handed a picture it cannot look at. llama.cpp
# is explicit about the missing projector; the rest are how other OpenAI-shaped
# servers phrase the same refusal.
_NO_VISION_SIGNS: Final = (
    "mmproj",
    "image input is not supported",
    "does not support image",
    "image input",
    "not a multimodal",
    "no vision",
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
class NoticeEvent:
    """Something the user should know about the turn, which is not the answer.

    A note rather than a failure: the turn continues and ends in an answer.
    """

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    code: str
    message: str


AgentEvent = (
    TokenEvent | ToolStartEvent | ToolEndEvent | FinalEvent | NoticeEvent | ErrorEvent
)


def chat_model(
    *,
    base_url: str,
    model: str,
    api_key: str | None = None,
    # The same seam `LlamaClient` has, for the same reason: without a transport
    # to hand in, nothing in a default test run ever builds this object, and
    # what it puts on the wire — the timeout's type, the token cap's spelling —
    # is exactly where this module has been wrong before.
    http: httpx2.AsyncClient | None = None,
) -> BaseChatModel:
    """llama.cpp through langchain-openai — the same server rung 3 points at."""
    return ChatOpenAI(
        base_url=base_url,
        model=model,
        http_async_client=http,
        # llama.cpp ignores the key unless started with `--api-key`; the SDK
        # underneath insists on one either way.
        api_key=SecretStr(api_key or "not-needed"),
        temperature=_TEMPERATURE,
        max_completion_tokens=_MAX_ANSWER_TOKENS,
        # langchain-openai sends `max_completion_tokens` alone. llama.cpp
        # aliased that late; older builds read only `max_tokens` and ignore
        # what they do not know, which would drop the cap silently. Both go on
        # the wire, so whichever the server understands is the same number.
        extra_body={"max_tokens": _MAX_ANSWER_TOKENS},
        # httpx2 is the transport the SDK actually runs on — an `httpx.Timeout`
        # here is a foreign object the client stores unparsed, and survives
        # only as long as the SDK keeps normalising it per request.
        timeout=httpx2.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
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
    bound = langchain_tools(context, tools)
    known = {tool.name for tool in bound}
    agent = create_agent(model=model, tools=bound, system_prompt=system)
    # A round is one model superstep and one tool superstep, plus the closing
    # model call that answers.
    config: RunnableConfig = {"recursion_limit": 2 * max_rounds + 1}

    attempt = history
    retried_without_images = False

    while True:
        spoken: list[str] = []
        last_answer = ""
        trace: list[dict[str, Any]] = []

        try:
            async for event in agent.astream_events(
                cast(Any, {"messages": attempt}), version="v2", config=config
            ):
                kind = event["event"]
                data = cast(dict[str, Any], event.get("data") or {})
                if kind == "on_chat_model_stream":
                    text = _text_of(data.get("chunk"))
                    if text:
                        spoken.append(text)
                        yield TokenEvent(text)
                elif kind == "on_chat_model_end":
                    output = data.get("output")
                    # The fallback for a model that answered without streaming.
                    last_answer = _text_of(output) or last_answer
                    # A call to a tool that does not exist is rejected before
                    # anything runs it, so no tool callback ever fires for it.
                    # The attempt is still part of the turn, and a turn the
                    # panel cannot see is what this trace exists to prevent.
                    for call in getattr(output, "tool_calls", None) or []:
                        name = str(call.get("name") or "")
                        if name in known:
                            continue
                        arguments = _arguments(call.get("args"))
                        call_id = str(call.get("id") or "")
                        yield ToolStartEvent(
                            call_id=call_id, name=name, arguments=arguments
                        )
                        summary = f"unknown tool {name}"
                        trace.append(
                            {
                                "name": name,
                                "arguments": arguments,
                                "ok": False,
                                "summary": summary,
                            }
                        )
                        yield ToolEndEvent(
                            call_id=call_id, name=name, ok=False, summary=summary
                        )
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
                elif kind == "on_tool_error":
                    # Arguments the schema rejects never reach the handler, so
                    # the wrapper's own never-raises guarantee does not cover
                    # them. Without this the start event above has no partner:
                    # the chip spins for the rest of the turn and the trace
                    # loses the call.
                    name = str(event.get("name") or "")
                    arguments = _arguments(data.get("input"))
                    summary = _first_line(str(data.get("error") or "the call failed"))
                    trace.append(
                        {
                            "name": name,
                            "arguments": arguments,
                            "ok": False,
                            "summary": summary,
                        }
                    )
                    yield ToolEndEvent(
                        call_id=str(event.get("run_id") or ""),
                        name=name,
                        ok=False,
                        summary=summary,
                    )
        except GraphRecursionError:
            # The note is appended, not a fallback: a model that narrated
            # before its first tool call has already said something, and
            # ending on that dangling sentence hides the fact that the budget
            # is what stopped it.
            said = "".join(spoken) or last_answer
            yield FinalEvent(
                content=f"{said}\n\n{_BUDGET_NOTE}" if said else _BUDGET_NOTE,
                tool_trace=trace,
            )
            return
        except Exception as error:
            # A text-only llama.cpp answers an image with a 500 and a hint about
            # `--mmproj`. That is a fact about the server, not a failure of the
            # question: drop the pictures, say so, and answer the words.
            if (
                not retried_without_images
                and not spoken
                and not trace
                and _rejects_images(error)
                and _has_images(attempt)
            ):
                retried_without_images = True
                attempt = _without_images(attempt)
                yield NoticeEvent("no_vision", _NO_VISION_NOTE)
                continue
            _log.warning("the model call failed: %s", error)
            yield ErrorEvent("model_unreachable", str(error))
            return

        yield FinalEvent(content="".join(spoken) or last_answer, tool_trace=trace)
        return


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


def _rejects_images(error: Exception) -> bool:
    return any(sign in str(error).lower() for sign in _NO_VISION_SIGNS)


def _has_images(history: list[dict[str, Any]]) -> bool:
    return any(_parts_of(turn) is not None for turn in history)


def _without_images(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same turns with the pictures taken out and the words kept."""
    plain: list[dict[str, Any]] = []
    for turn in history:
        parts = _parts_of(turn)
        if parts is None:
            plain.append(turn)
            continue
        text = "".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
        plain.append({**turn, "content": text})
    return plain


def _parts_of(turn: dict[str, Any]) -> list[Any] | None:
    """The content blocks of a turn that carries an image, or None."""
    content = turn.get("content")
    if not isinstance(content, list):
        return None
    pictured = any(
        isinstance(part, dict) and part.get("type") in {"image_url", "image"}
        for part in content
    )
    return content if pictured else None


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
    stripped = text.strip()
    return (not failed and bool(stripped), _first_line(stripped))


def _first_line(text: str) -> str:
    """One line, safe to persist and to print. Blank in, blank out — a string
    of spaces is truthy and has no lines, and indexing it ends the turn."""
    lines = text.strip().splitlines()
    return lines[0][:120] if lines else ""
