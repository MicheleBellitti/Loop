"""The wire contract.

The success condition for this API is that the existing client, unmodified,
points at it and works — so these tests assert the shape of the JSON rather than
the behaviour behind it. A renamed key, a null where the reference sends an
absent one, a number where it sends a string: each of those breaks a browser and
none of them breaks a handler.
"""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import SOME_TUESDAY, connect_mailbox
from httpx import AsyncClient

from loop.api import auth
from loop.api.serialise import confidence, iso_z, num, quoted
from loop.db import Database, Queue

pytestmark = pytest.mark.integration


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


class TestOneApplication:
    async def test_the_detail_is_the_row_plus_facts_and_events(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)

        body = (await client.get(f"/api/applications/{application_id}")).json()

        rows = (await client.get("/api/applications?limit=200")).json()["rows"]
        row = next(r for r in rows if r["id"] == application_id)
        # The drawer and the row it opened from cannot disagree: the first
        # seventeen keys are the same seventeen values.
        assert {k: body[k] for k in row} == row
        assert list(body)[-2:] == ["facts", "events"]

    async def test_an_id_the_reference_would_refuse_is_refused_here(
        self, client: AsyncClient
    ) -> None:
        # Python's `uuid.UUID` accepts braces and no dashes; the reference's
        # regex does not, and a route that accepts more ids than the reference
        # 404s where the reference 400s.
        response = await client.get("/api/applications/0193f26b1e7c8a9d4e1f2a3b4c5d6e7f")
        assert response.status_code == 400
        assert response.json()["error"] == {
            "code": "bad_id",
            "message": "that is not an application id",
            "field": "id",
        }

    async def test_an_unknown_application_is_a_404_with_no_field_key(
        self, client: AsyncClient
    ) -> None:
        response = await client.get(
            "/api/applications/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404
        # `field` is absent rather than null: the reference passes undefined and
        # JSON.stringify drops it.
        assert response.json()["error"] == {
            "code": "not_found",
            "message": "no such application",
        }

    async def test_facts_are_present_even_when_there_are_none(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(db, user_id)
        facts = (await client.get(f"/api/applications/{application_id}")).json()["facts"]
        assert list(facts) == [
            "applied",
            "ats",
            "posting_url",
            "location",
            "posted_range",
            "offers",
        ]
        assert facts["posted_range"] is None
        assert facts["offers"] == []


class TestTheReviewQueue:
    async def test_the_envelope_is_items_and_the_keys_are_the_select_list(
        self, client: AsyncClient
    ) -> None:
        body = (await client.get("/api/review")).json()
        assert list(body) == ["items"]
        for item in body["items"]:
            assert list(item) == [
                "id",
                "kind",
                "evidence_ref",
                "excerpt",
                "candidates",
                "application_id",
                "created_at",
                "expires_at",
            ]
            # Nested JSON, not a string — the client reads the choices off it.
            assert isinstance(item["candidates"], list)


class TestTheStatistics:
    async def test_the_ten_sections_are_always_all_ten(self, client: AsyncClient) -> None:
        body = (await client.get("/api/stats")).json()
        assert list(body) == [
            "period",
            "funnel",
            "ratios",
            "first_response",
            "ghost",
            "channels",
            "channel_note",
            "time_in_stage",
            "compensation",
            "seasonal",
        ]

    async def test_an_unknown_period_degrades_rather_than_failing(
        self, client: AsyncClient
    ) -> None:
        assert (await client.get("/api/stats?period=banana")).json()["period"] == "12m"
        assert (await client.get("/api/stats?period=90d")).json()["period"] == "90d"

    async def test_the_funnel_is_five_bars_and_the_first_is_the_scale(
        self, client: AsyncClient
    ) -> None:
        funnel = (await client.get("/api/stats")).json()["funnel"]
        assert [bar["label"] for bar in funnel] == [
            "Applied",
            "Acknowledged",
            "Screening",
            "Interviewing",
            "Offer",
        ]
        assert funnel[0]["width"] in {0, 100}

    async def test_every_ratio_carries_the_denominator_it_was_computed_from(
        self, client: AsyncClient
    ) -> None:
        for metric in (await client.get("/api/stats")).json()["ratios"]:
            assert list(metric) == [
                "label",
                "value",
                "numerator",
                "denominator",
                "excluded",
                "gate_met",
                "note",
                "small_sample",
                "display",
            ]
            # A ratio below its gate is null and says what unlocks it, rather
            # than showing a number three applications wide.
            assert metric["value"] is not None or not metric["gate_met"] or (
                metric["denominator"] == 0
            )
            assert metric["note"]

    async def test_a_channel_below_its_gate_shows_dashes_not_numbers(
        self, client: AsyncClient
    ) -> None:
        for channel in (await client.get("/api/stats")).json()["channels"]:
            if not channel["gate_met"]:
                assert (channel["iv"], channel["of"], channel["ghost"]) == ("—", "—", "—")
                assert channel["note"].endswith("needed")


class TestTheMailboxHealth:
    """The shape `App.tsx` reads before it renders anything at all.

    Why this route answers a status rather than the list its name promises is
    argued once, in `loop.api.mailbox`.
    """

    @staticmethod
    async def _set(db: Database, user_id: str, mailbox_id: str, assignments: str) -> None:
        async with db.session(user_id) as connection:
            await connection.execute(
                f"update mailbox_accounts set {assignments} where id = $1", mailbox_id
            )

    async def test_it_is_the_same_object_today_carries(
        self, client: AsyncClient, mailbox_id: str
    ) -> None:
        health = (await client.get("/api/mailboxes")).json()
        assert list(health) == [
            "connected",
            "providers",
            "last_ok_at",
            "minutes_since_read",
            "placed_today",
            "backlog",
            "state",
        ]
        # The entries, not just the envelope: the shell keys its list on `id`
        # and labels it with `provider` and `address`, so a rename inside the
        # array is the same white screen one level down.
        assert list(health["providers"][0]) == [
            "id",
            "provider",
            "address",
            "status",
            "last_ok_at",
        ]
        assert health == (await client.get("/api/today")).json()["mailbox_health"]

    async def test_providers_is_a_list_even_with_no_mailbox(self, client: AsyncClient) -> None:
        health = (await client.get("/api/mailboxes")).json()
        assert health["providers"] == []
        assert health["connected"] is False
        assert health["state"] == "ok"

    async def test_a_revoked_grant_is_the_one_full_screen_failure(
        self, client: AsyncClient, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        await self._set(db, user_id, mailbox_id, "status = 'needs_reauth'")
        health = (await client.get("/api/mailboxes")).json()
        assert health["state"] == "F1"
        # A row exists and cannot be read, which is not connected.
        assert health["connected"] is False

    async def test_a_mailbox_that_is_failing_is_not_a_mailbox_that_is_fine(
        self, client: AsyncClient, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        # The connector writes 'error' for every failure that is not an auth
        # failure — a quota ceiling, a run of 5xx. Nothing is being read, so
        # `connected` cannot say yes; but it is not revoked, so it is not the
        # full screen either.
        await self._set(db, user_id, mailbox_id, "status = 'error', last_ok_at = now()")
        health = (await client.get("/api/mailboxes")).json()
        assert health["connected"] is False
        assert health["state"] == "ok"
        assert health["providers"][0]["status"] == "error"

    async def test_one_lapsed_grant_of_two_does_not_blank_the_app(
        self, client: AsyncClient, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        # F1 replaces the entire product. A user whose work mailbox still reads
        # is a degraded account, not an unreachable one.
        await self._set(db, user_id, mailbox_id, "status = 'needs_reauth', last_ok_at = now()")
        await connect_mailbox(
            db, user_id, address="work@pytest.invalid", last_ok_at=datetime.now(UTC)
        )
        health = (await client.get("/api/mailboxes")).json()
        assert health["state"] == "ok"
        assert health["connected"] is True

    async def test_a_backlog_is_a_state_of_its_own(
        self, client: AsyncClient, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        await self._set(db, user_id, mailbox_id, "backlog_estimate = 40, last_ok_at = now()")
        health = (await client.get("/api/mailboxes")).json()
        assert (health["state"], health["backlog"]) == ("F2", 40)
        assert health["connected"] is True

    async def test_freshness_is_the_worst_provider_not_the_best(
        self, client: AsyncClient, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        # A mailbox read a minute ago and one last read a week ago is an account
        # that is a week stale. Reporting the newest would call it healthy
        # exactly when half of it had stopped.
        await self._set(db, user_id, mailbox_id, "last_ok_at = now()")
        await connect_mailbox(
            db,
            user_id,
            address="work@pytest.invalid",
            last_ok_at=datetime.now(UTC) - timedelta(days=7),
        )
        health = (await client.get("/api/mailboxes")).json()
        # A minute of slack: the row is stamped here and the delta is measured
        # in the API process, and the two clocks are not the same clock.
        assert health["minutes_since_read"] >= 7 * 24 * 60 - 1

    async def test_a_mailbox_that_has_never_read_is_the_worst_of_all(
        self, client: AsyncClient, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        # Never read is worse than any timestamp, so it cannot be filtered out
        # on the way to the minimum — that answers with the healthy half.
        await self._set(db, user_id, mailbox_id, "last_ok_at = now()")
        await connect_mailbox(db, user_id, address="work@pytest.invalid")
        health = (await client.get("/api/mailboxes")).json()
        assert health["last_ok_at"] is None
        assert health["minutes_since_read"] is None

    async def test_it_counts_what_was_placed_today(
        self, client: AsyncClient, db: Database, user_id: str, mailbox_id: str
    ) -> None:
        # The one number here that no other assertion would notice going to
        # zero, and the number onboarding shows while it is earning trust.
        async with db.session(user_id) as connection:
            for index, (outcome, processed_at) in enumerate(
                [
                    ("placed", datetime.now(UTC)),
                    ("placed", datetime.now(UTC) - timedelta(days=2)),
                    ("dropped", datetime.now(UTC)),
                ]
            ):
                await connection.execute(
                    """
                    insert into seen_messages
                      (mailbox_id, provider_message_id, user_id, body_sha256,
                       received_at, outcome, processed_at)
                    values ($1,$2,$3,$4,$5,$6,$7)
                    """,
                    mailbox_id,
                    f"placed-{index}",
                    user_id,
                    b"\x00" * 32,
                    SOME_TUESDAY,
                    outcome,
                    processed_at,
                )
        health = (await client.get("/api/mailboxes")).json()
        assert health["placed_today"] == 1


class TestIsItStillReadingMyMail:
    async def test_the_deep_check_needs_no_session(self, anonymous: AsyncClient) -> None:
        # A health check you have to sign in for cannot tell you why you cannot
        # sign in.
        body = (await anonymous.get("/health/deep")).json()
        assert list(body) == [
            "ok",
            "queues",
            "oldest_unprocessed_seconds",
            "dead_letters",
            "mailbox_staleness_hours",
            "components",
        ]
        assert set(body["components"]) == {
            "template_rules",
            "calendar_detection",
            "local_model",
        }
        assert set(body["queues"]) == set(Queue.ALL)

    async def test_the_push_key_is_null_rather_than_an_error(
        self, client: AsyncClient
    ) -> None:
        assert (await client.get("/api/push/key")).json() == {"public_key": None}


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


class TestEveryFailureWearsTheSameEnvelope:
    """`{"error": {"code": …}}`, whoever produced it.

    The client reads `error.code` off every failure and nothing else, so a
    response that answers in another shape is a failure it cannot report — it
    falls through to "something went wrong" with the reason on the wire and
    unread.
    """

    async def test_a_parameter_fastapi_rejects_still_answers_in_it(
        self, client: AsyncClient
    ) -> None:
        # `limit` is `int` with bounds, so this never reaches the handler:
        # FastAPI raises before it, and its own handler answers `{"detail": …}`.
        response = await client.get("/api/applications?limit=nonsense")
        assert response.status_code == 422
        assert "detail" not in response.json()
        assert response.json()["error"]["code"] == "bad_request"
        assert response.json()["error"]["field"] == "limit"

    async def test_a_parameter_out_of_range_names_the_field_too(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/api/applications?limit=0")
        assert response.status_code == 422
        assert response.json()["error"]["field"] == "limit"

    async def test_it_carries_the_same_security_headers_as_a_success(
        self, client: AsyncClient
    ) -> None:
        failed = await client.get("/api/applications?limit=nonsense")
        assert failed.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in failed.headers

    async def test_a_500_carries_them_too(self, dsn: str, user_id: str) -> None:
        """The one response that used to leave without a policy.

        Starlette hangs a bare `Exception` handler off `ServerErrorMiddleware`,
        which wraps the user middleware rather than sitting inside it — so the
        headers applied on the way out never touched a 500. It needs its own
        route to reach, because no handler in the product raises on demand.
        """
        from httpx import ASGITransport, AsyncClient

        from loop.api import Settings, create_app

        app = create_app(Settings(dsn=dsn, session_secret="test-secret"))

        @app.get("/api/boom")
        async def _boom() -> None:
            raise RuntimeError("the database fell over")

        async with app.router.lifespan_context(app):
            token, _session = await app.state.sessions.create(user_id)
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
                cookies={auth.COOKIE_NAME: token},
            ) as http:
                response = await http.get("/api/boom")

        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": "internal", "message": "something failed"}
        }
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in response.headers
