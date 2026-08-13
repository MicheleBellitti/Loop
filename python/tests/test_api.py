"""The wire contract.

The success condition for this API is that the existing client, unmodified,
points at it and works — so these tests assert the shape of the JSON rather than
the behaviour behind it. A renamed key, a null where the reference sends an
absent one, a number where it sends a string: each of those breaks a browser and
none of them breaks a handler.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from loop.api import Settings, auth, create_app
from loop.api.serialise import confidence, iso_z, num, quoted
from loop.db import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(dsn: str, user_id: str) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(dsn=dsn, session_secret="test-secret"))
    async with app.router.lifespan_context(app):
        token, _session = await app.state.sessions.create(user_id)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={auth.COOKIE_NAME: token},
        ) as http:
            http.app = app  # type: ignore[attr-defined]
            yield http


@pytest.fixture
async def anonymous(dsn: str) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(dsn=dsn, session_secret="test-secret"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http


class TestTheGate:
    async def test_a_public_path_needs_nothing(self, anonymous: AsyncClient) -> None:
        response = await anonymous.get("/api/auth/state")
        assert response.status_code == 200
        assert set(response.json()) == {"seeded", "has_passkey"}

    async def test_everything_else_needs_a_session(self, anonymous: AsyncClient) -> None:
        response = await anonymous.get("/api/applications")
        assert response.status_code == 401
        assert response.json() == {
            "error": {"code": "unauthenticated", "message": "sign in first"}
        }

    async def test_an_unknown_api_path_is_401_before_it_is_404(
        self, anonymous: AsyncClient
    ) -> None:
        # The gate runs first, so an anonymous request cannot even learn which
        # endpoints exist.
        assert (await anonymous.get("/api/nothing-here")).status_code == 401

    async def test_and_404_once_you_are_in(self, client: AsyncClient) -> None:
        response = await client.get("/api/nothing-here")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_a_mutation_without_the_csrf_token_is_refused(
        self, client: AsyncClient
    ) -> None:
        response = await client.post("/api/auth/logout")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf"

    async def test_and_accepted_with_it(self, client: AsyncClient) -> None:
        token = (await client.get("/api/me")).json()["csrf"]
        response = await client.post("/api/auth/logout", headers={"x-csrf-token": token})
        assert response.status_code == 200
        assert "loop_session=;" in response.headers["set-cookie"]

    async def test_every_response_carries_the_policy(self, anonymous: AsyncClient) -> None:
        # Including the ones the gate turns away: a 401 is still a response the
        # browser renders something for.
        response = await anonymous.get("/api/applications")
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"


class TestTheSession:
    async def test_me_carries_the_five_keys_the_shell_needs(self, client: AsyncClient) -> None:
        body = (await client.get("/api/me")).json()
        assert list(body) == ["email", "tz", "locale", "display_currency", "csrf"]
        assert len(body["csrf"]) == 43

    async def test_the_csrf_token_is_derived_and_survives_a_reload(
        self, client: AsyncClient
    ) -> None:
        # Stored nowhere, recomputed from the session id, so a client that
        # dropped it can ask again and get the same one.
        first = (await client.get("/api/me")).json()["csrf"]
        second = (await client.get("/api/me")).json()["csrf"]
        assert first == second

    async def test_the_cookie_is_written_the_way_the_reference_writes_it(self) -> None:
        cookie = auth.session_cookie("tok", secure=False)
        assert cookie == "loop_session=tok; Max-Age=2592000; Path=/; HttpOnly; SameSite=Lax"
        # Secure sits between HttpOnly and SameSite, not on the end.
        assert auth.session_cookie("tok", secure=True).endswith(
            "HttpOnly; Secure; SameSite=Lax"
        )


class TestThePipelineBoard:
    async def test_every_row_carries_all_seventeen_keys(self, client: AsyncClient) -> None:
        body = (await client.get("/api/applications?limit=200")).json()
        assert set(body) == {"rows", "next_cursor"}
        assert body["next_cursor"] is None
        for row in body["rows"]:
            assert list(row) == [
                "id",
                "company",
                "role",
                "stage",
                "display_stage",
                "phase",
                "status",
                "channel",
                "applied_at",
                "last_signal_at",
                "days_quiet",
                "quiet_label",
                "flag",
                "flag_kind",
                "closed",
                "needs_review",
                "confidence",
            ]

    async def test_the_stage_arrives_as_both_a_key_and_a_label(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        await _an_application(db, user_id)
        rows = (await client.get("/api/applications?limit=200")).json()["rows"]
        row = next(r for r in rows if r["company"] == "Prima")
        assert row["stage"] == "applied"
        assert row["display_stage"] == "Applied"

    async def test_a_quiet_application_says_how_quiet_in_words(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        await _an_application(db, user_id, last_signal_days_ago=12)
        rows = (await client.get("/api/applications?limit=200")).json()["rows"]
        row = next(r for r in rows if r["company"] == "Prima")
        assert row["days_quiet"] == 12
        assert row["quiet_label"] == "quiet 12 days"

    async def test_and_one_that_has_never_been_heard_from_says_nothing(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        await _an_application(db, user_id, last_signal_days_ago=None)
        rows = (await client.get("/api/applications?limit=200")).json()["rows"]
        row = next(r for r in rows if r["company"] == "Prima")
        # Null rather than absent, and an empty string rather than null.
        assert row["days_quiet"] is None
        assert row["quiet_label"] == ""

    async def test_a_phase_filter_narrows_and_all_does_not(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        await _an_application(db, user_id)
        everything = (await client.get("/api/applications?phase=all&limit=200")).json()
        decided = (await client.get("/api/applications?phase=decided&limit=200")).json()
        assert len(everything["rows"]) >= 1
        assert all(r["phase"] == "decided" for r in decided["rows"])


class TestToday:
    async def test_every_key_is_present_even_when_empty(self, client: AsyncClient) -> None:
        body = (await client.get("/api/today")).json()
        assert list(body) == [
            "eyebrow",
            "headline",
            "headline_kind",
            "counters",
            "review_count",
            "next_interview",
            "suggestions",
            "recent_events",
            "mailbox_health",
            "closing_line",
        ]
        # Null rather than absent when there is nothing scheduled.
        assert body["next_interview"] is None or isinstance(body["next_interview"], dict)
        assert isinstance(body["suggestions"], list)

    async def test_the_server_writes_the_words(self, client: AsyncClient) -> None:
        body = (await client.get("/api/today")).json()
        # The client renders these verbatim; formatting any of them in the
        # browser would put the stage machine there too.
        assert isinstance(body["eyebrow"], str) and body["eyebrow"]
        assert isinstance(body["headline"], list)
        assert body["headline_kind"] in {"moved", "empty", "clear", "waiting"}

    async def test_the_counters_are_numbers(self, client: AsyncClient) -> None:
        counters = (await client.get("/api/today")).json()["counters"]
        assert set(counters) == {"live", "interviewing", "offer", "overdue"}
        assert all(isinstance(v, int) for v in counters.values())


class TestTheJavascriptHabits:
    """Three ways Python would otherwise write JSON the client cannot read."""

    def test_a_whole_number_loses_its_decimal_point(self) -> None:
        # JavaScript has one number type: `1.0` goes over the wire as `1`.
        assert num(1.0) == 1
        assert num(0.82) == 0.82
        assert num(None) is None

    def test_a_timestamp_ends_in_z_with_milliseconds(self) -> None:
        assert iso_z(datetime(2026, 8, 13, 9, 41, 7, 482000, tzinfo=UTC)) == (
            "2026-08-13T09:41:07.482Z"
        )
        assert iso_z(None) is None

    def test_a_bigint_stays_a_string_and_a_confidence_keeps_two_decimals(self) -> None:
        # Accidents of the reference's driver, and part of the contract anyway.
        assert quoted(42) == "42"
        assert confidence(1) == "1.00"


async def _an_application(
    db: Database, user_id: str, *, last_signal_days_ago: int | None = 1
) -> str:
    from datetime import timedelta

    last_signal = (
        datetime.now(UTC) - timedelta(days=last_signal_days_ago)
        if last_signal_days_ago is not None
        else None
    )
    async with db.session(user_id) as connection:
        company = await connection.fetchval(
            """
            insert into companies (canonical_name, domain) values ('Prima','prima.it')
            on conflict (lower(canonical_name), coalesce(domain, '')) do update
              set canonical_name = excluded.canonical_name
            returning id
            """
        )
        return str(
            await connection.fetchval(
                """
                insert into applications
                  (user_id, company_id, role_title, current_stage, current_phase,
                   confidence, last_signal_at)
                values ($1,$2,'Machine Learning Engineer','applied','sent',1.0,$3)
                returning id
                """,
                user_id,
                company,
                last_signal,
            )
        )
