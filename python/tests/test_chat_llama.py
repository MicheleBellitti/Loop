"""The model-server client — now the openai SDK with our error taxonomy.

The SDK's own streaming and parsing are its maintainers' problem to test
(decisions.md LIB-1); what is ours is the seam: the base URL reaches the
right path, the id list comes back as ids, and a refusal becomes the one
`LlamaError` the routes catch.
"""

from collections.abc import Callable

import httpx2
import pytest

from loop.chat.llama import LlamaClient, LlamaError


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response], api_key: str | None = None
) -> LlamaClient:
    return LlamaClient(
        "http://llama.test/v1",
        api_key=api_key,
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


class TestModels:
    async def test_the_served_list_comes_back_as_ids(self) -> None:
        def handle(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == "/v1/models"
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "qwen", "object": "model", "created": 0, "owned_by": "loop"},
                        {"id": "llava", "object": "model", "created": 0, "owned_by": "loop"},
                    ],
                },
            )

        assert await _client(handle).models() == ["qwen", "llava"]

    async def test_a_refusal_is_an_error_the_routes_can_catch(self) -> None:
        def handle(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(503, json={"error": "the model is still loading"})

        with pytest.raises(LlamaError):
            await _client(handle).models()

    async def test_an_api_key_travels_as_a_bearer(self) -> None:
        seen: list[str] = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.headers.get("authorization", ""))
            return httpx2.Response(200, json={"object": "list", "data": []})

        await _client(handle, api_key="sk-local").models()
        assert seen == ["Bearer sk-local"]
