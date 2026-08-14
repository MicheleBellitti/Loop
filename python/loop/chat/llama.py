"""llama.cpp, over the OpenAI wire format, streamed.

The extractor's rung 3 makes one unstreamed call, schema-enforced, no tools,
then abstains — that contract is load-bearing for the pipeline and lives in the
extractor. This client is the other thing the same server offers: a
conversation, token by token, with tool calls. It is written against llama.cpp's
`llama-server` (`--jinja` enables the tool grammar) but speaks plain
`/chat/completions`, so anything OpenAI-compatible serves.

Parsing is deliberately tolerant. Engines disagree on the small print of
streaming tool calls — some send the name first and the arguments in fragments,
some send the whole call in one chunk — so calls are accumulated by index and
read only when the stream ends.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

import httpx

# Chat wants first tokens fast and whole answers eventually; a local model on
# modest hardware can legitimately take minutes over a long answer, so the read
# timeout is generous and the connect timeout is not.
_CONNECT_TIMEOUT_SECONDS: Final = 10.0
_READ_TIMEOUT_SECONDS: Final = 300.0

_DONE: Final = "[DONE]"


class LlamaError(Exception):
    """The model server did not answer, or answered with a failure."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One call the model asked for. `arguments` is raw JSON text — the model
    wrote it, so parsing it is the agent's job and failure is an answer."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class TokenDelta:
    """A fragment of assistant text, in arrival order."""

    text: str


@dataclass(frozen=True, slots=True)
class Completion:
    """The whole turn, once the stream has ended."""

    content: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str


@dataclass(slots=True)
class _GrowingCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class LlamaClient:
    """One llama.cpp server, by base URL — `http://localhost:8080/v1`-shaped."""

    base_url: str
    api_key: str | None = None
    http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    _READ_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS
                )
            )
        return self.http

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def models(self) -> list[str]:
        """What the server offers. One id for a single-model server; several
        when llama.cpp runs as a router."""
        response = await self._client().get(
            f"{self.base_url.rstrip('/')}/models", headers=self._headers()
        )
        if not response.is_success:
            raise LlamaError(f"the model server returned {response.status_code}")
        body = response.json()
        found = [
            str(entry.get("id"))
            for entry in (body.get("data") or ())
            if isinstance(entry, dict) and entry.get("id")
        ]
        return found

    async def stream_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> AsyncIterator[TokenDelta | Completion]:
        """Yield text fragments as they arrive, then exactly one Completion.

        Raises `LlamaError` for anything that is not a well-formed stream: an
        unreachable server, a non-200, a stream that ends mid-air.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        content: list[str] = []
        calls: dict[int, _GrowingCall] = {}
        finish_reason = ""

        try:
            async with self._client().stream(
                "POST",
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as response:
                if not response.is_success:
                    # The body is read so the error can say why; capped because
                    # a llama.cpp failure page can carry the whole prompt back.
                    raised = (await response.aread())[:300]
                    raise LlamaError(
                        f"the model server returned {response.status_code}: "
                        f"{raised.decode('utf-8', errors='replace')}"
                    )
                async for line in response.aiter_lines():
                    data = _data_of(line)
                    if data is None:
                        continue
                    if data == _DONE:
                        break
                    text, reason = _read_chunk(data, calls)
                    if reason:
                        finish_reason = reason
                    if text:
                        content.append(text)
                        yield TokenDelta(text)
        except httpx.HTTPError as error:
            raise LlamaError(f"the model server is unreachable: {error}") from error

        yield Completion(
            content="".join(content),
            tool_calls=tuple(
                ToolCall(id=c.id or f"call_{i}", name=c.name, arguments=c.arguments)
                for i, c in sorted(calls.items())
                if c.name
            ),
            finish_reason=finish_reason,
        )


def _data_of(line: str) -> str | None:
    """The payload of one SSE line, or None for the frames that carry none."""
    stripped = line.strip()
    if not stripped or stripped.startswith(":") or not stripped.startswith("data:"):
        return None
    return stripped[5:].strip()


def _read_chunk(data: str, calls: dict[int, _GrowingCall]) -> tuple[str, str]:
    """Fold one chunk into the accumulators; return (text, finish_reason)."""
    try:
        chunk = json.loads(data)
    except ValueError:
        # One malformed frame is not worth ending the conversation over.
        return "", ""
    choices = chunk.get("choices") or ()
    if not choices:
        return "", ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") or {}

    for entry in delta.get("tool_calls") or ():
        if not isinstance(entry, dict):
            continue
        index = int(entry.get("index") or 0)
        growing = calls.setdefault(index, _GrowingCall())
        if entry.get("id"):
            growing.id = str(entry["id"])
        function = entry.get("function") or {}
        if function.get("name"):
            growing.name += str(function["name"])
        if function.get("arguments"):
            growing.arguments += str(function["arguments"])

    text = delta.get("content")
    return (
        text if isinstance(text, str) else "",
        str(choice.get("finish_reason") or ""),
    )
