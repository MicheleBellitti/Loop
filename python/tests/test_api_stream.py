"""The open connection, the download, and the scrape.

The stream test is the one worth having: it asserts that a change written by the
pipeline reaches a browser, which is the whole reason the pipeline calls
`pg_notify` after it commits rather than before.
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from loop.api import auth
from loop.db import Database
from loop.domain.messages import PendingEvent
from loop.services import PipelineService

pytestmark = pytest.mark.integration


class TestTheStream:
    """Over a real socket, because the in-process transport cannot do this.

    httpx's ASGI transport runs the application to completion before it returns
    a response, and a stream never completes — so an SSE endpoint tested through
    it deadlocks rather than failing. This one runs uvicorn on a port, which is
    also the only way to assert the headers a proxy will actually see.
    """

    async def test_a_change_reaches_the_browser(
        self, served: str, db: Database, user_id: str, session_cookie: str
    ) -> None:
        frames: list[str] = []

        async def read() -> None:
            async with (
                AsyncClient(
                    base_url=served, cookies={auth.COOKIE_NAME: session_cookie}
                ) as http,
                http.stream("GET", "/api/stream", timeout=10) as response,
            ):
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                # Without this a proxy holds the frames until it has enough of
                # them to be worth forwarding, which is to say forever.
                assert response.headers["x-accel-buffering"] == "no"
                async for line in response.aiter_lines():
                    frames.append(line)
                    if line.startswith("data:"):
                        return

        reader = asyncio.create_task(read())
        await asyncio.sleep(0.3)

        application_id = await _an_application(db, user_id)
        await PipelineService(db).apply(_an_event(user_id, application_id))

        async with asyncio.timeout(10):
            await reader

        assert frames[0] == ": connected"
        assert "event: application.changed" in frames
        payload = json.loads(next(f for f in frames if f.startswith("data:"))[5:])
        assert payload["application_id"] == application_id
        assert payload["user_id"] == user_id

    async def test_it_needs_a_session_like_everything_else(
        self, anonymous: AsyncClient
    ) -> None:
        assert (await anonymous.get("/api/stream")).status_code == 401


class TestTheExport:
    async def test_the_json_carries_the_whole_account(self, client: AsyncClient) -> None:
        response = await client.get("/api/export")
        assert response.headers["content-disposition"] == (
            'attachment; filename="loop-export.json"'
        )
        assert set(response.json()) == {
            "applications",
            "events",
            "sources",
            "interviews",
            "comp_offers",
            "deadlines",
            "review_items",
            "suggestions",
            "stage_defs",
        }
        # The stage keys in `applications` are resolvable against these, which
        # is what the reference's export left out.
        assert response.json()["stage_defs"]

    async def test_no_internal_representation_leaves_the_building(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        await _an_application(db, user_id)
        rows = (await client.get("/api/export")).json()["applications"]
        assert rows
        assert all("role_embedding" not in row for row in rows)

    async def test_a_column_that_is_not_a_scalar_still_serialises(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)
        async with db.session(user_id) as connection:
            await connection.execute(
                """
                insert into suggestions (user_id, key, rule, application_ids, payload)
                values ($1,'let_it_go:x','let_it_go',$2,'{}')
                """,
                user_id,
                [application_id],
            )

        response = await client.get("/api/export")

        # `application_ids` is a `uuid[]`, and a conversion that only looked at
        # the top level worked on every account with no suggestions — which was
        # every account this was tested against and not the one it ran on.
        assert response.status_code == 200
        assert response.json()["suggestions"][0]["application_ids"] == [application_id]

    async def test_an_empty_csv_is_still_a_table(self, client: AsyncClient) -> None:
        response = await client.get("/api/export?format=csv")
        assert response.headers["content-type"].startswith("text/csv")
        # A header row rather than an empty file: what opens in a spreadsheet is
        # a table with no rows, not a document with no columns.
        assert response.text.splitlines()[0].startswith("id,company,role_title")

    async def test_anything_that_is_not_csv_is_json(self, client: AsyncClient) -> None:
        assert (await client.get("/api/export?format=CSV")).headers[
            "content-type"
        ].startswith("application/json")


class TestTheScrape:
    async def test_it_is_prometheus_and_it_is_public(
        self, anonymous: AsyncClient
    ) -> None:
        response = await anonymous.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; version=0.0.4"
        assert "# TYPE queue_depth gauge" in response.text
        assert 'queue_depth{queue="event_pending"}' in response.text
        assert response.text.endswith("\n")


class TestSubscribing:
    async def test_a_device_is_recorded_once_however_often_it_asks(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        token = (await client.get("/api/me")).json()["csrf"]
        body = {
            "endpoint": "https://push.example/abc",
            "keys": {"p256dh": "a-key", "auth": "a-secret"},
            # The browser sends this and nothing reads it.
            "expirationTime": None,
        }
        for _ in range(2):
            response = await client.post(
                "/api/push/subscribe", json=body, headers={"x-csrf-token": token}
            )
            assert response.json() == {"ok": True}

        async with db.session(user_id) as connection:
            assert (
                await connection.fetchval(
                    "select count(*) from push_subscriptions where user_id = $1", user_id
                )
                == 1
            )

    async def test_a_subscription_without_keys_is_refused(
        self, client: AsyncClient
    ) -> None:
        token = (await client.get("/api/me")).json()["csrf"]
        response = await client.post(
            "/api/push/subscribe",
            json={"endpoint": "https://push.example/abc"},
            headers={"x-csrf-token": token},
        )
        assert response.status_code == 400
        assert response.json()["error"]["field"] == "keys"


async def _an_application(db: Database, user_id: str) -> str:
    async with db.session(user_id) as connection:
        company = await connection.fetchval(
            """
            insert into companies (canonical_name) values ('Prima')
            on conflict (lower(canonical_name), coalesce(domain, '')) do update
              set canonical_name = excluded.canonical_name
            returning id
            """
        )
        return str(
            await connection.fetchval(
                """
                insert into applications
                  (user_id, company_id, role_title, current_stage, current_phase, confidence)
                values ($1,$2,'Engineer','applied','sent',1.0)
                returning id
                """,
                user_id,
                company,
            )
        )


def _an_event(user_id: str, application_id: str) -> PendingEvent:
    return PendingEvent(
        user_id=user_id,
        application_id=application_id,
        type="acknowledged",
        occurred_at=datetime.now(UTC),
        confidence=0.95,
        to_stage="acknowledged",
        evidence_ref="stream-1",
        rung=1,
    )
