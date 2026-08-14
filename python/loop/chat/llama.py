"""The model server, through the openai SDK.

llama.cpp's `llama-server` speaks the OpenAI wire format, and the SDK that
format belongs to is maintained, typed and tested against every quirk of it —
so it is used rather than re-implemented (decisions.md LIB-1). What this
module keeps of its own is exactly what the SDK cannot know: the error
taxonomy this product wants (one `LlamaError` the routes can catch) and the
generous read timeout a local model on modest hardware needs.
"""

from typing import Final

import httpx2
import openai

# Chat wants first tokens fast and whole answers eventually; a local model can
# legitimately take minutes over a long answer, so the read timeout is generous
# and the connect timeout is not.
CONNECT_TIMEOUT_SECONDS: Final = 10.0
READ_TIMEOUT_SECONDS: Final = 300.0

# llama.cpp ignores the key unless started with `--api-key`; the SDK insists
# on one either way.
_NO_KEY: Final = "not-needed"


class LlamaError(Exception):
    """The model server did not answer, or answered with a failure."""


class LlamaClient:
    """One OpenAI-compatible server, by base URL — `http://host:8080/v1`-shaped."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        # httpx2 is the SDK's own transport, which is also what makes this
        # testable: the tests hand in a client over a MockTransport.
        http: httpx2.AsyncClient | None = None,
    ) -> None:
        self._client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or _NO_KEY,
            timeout=httpx2.Timeout(
                READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            ),
            max_retries=1,
            http_client=http,
        )

    async def models(self) -> list[str]:
        """What the server offers. One id for a single-model llama.cpp; several
        when it runs as a router."""
        try:
            page = await self._client.models.list()
        except openai.OpenAIError as error:
            raise LlamaError(f"the model server did not answer: {error}") from error
        return [model.id for model in page.data]

    async def aclose(self) -> None:
        await self._client.close()
