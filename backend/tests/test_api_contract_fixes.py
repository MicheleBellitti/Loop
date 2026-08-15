"""Four places the port had drifted from the contract it promised to keep.

P3's success condition is that the existing PWA, unmodified, works against this
API. These are the four things an audit against the reference turned up that it
did not — three of which no test would have caught, because each one produces a
working page that is quietly wrong.
"""

import base64
import inspect
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from loop.api import Settings, create_app
from loop.api.routes.applications import _DEFAULT_LIMIT, _SORTS, list_applications
from loop.db import Database

pytestmark = pytest.mark.integration


class TestTheBoardsDefaults:
    async def test_asking_for_no_limit_gets_a_hundred(self, client: AsyncClient) -> None:
        # `client/src/mobile/Pipeline.tsx` is the one caller that sends no
        # `limit` at all. The port's default was 50, so the mobile board showed
        # half a pipeline and said nothing about the other half.
        #
        # Read off the route's own signature, not compared to the constant:
        # `assert _DEFAULT_LIMIT == 100` is the constant asserting about
        # itself, and stays true while the handler's `Query(default=50)`
        # quietly halves the board again.
        limit = inspect.signature(list_applications).parameters["limit"]
        assert limit.default.default == _DEFAULT_LIMIT == 100
        response = await client.get("/api/applications")
        assert response.status_code == 200

    async def test_two_reads_of_a_tie_come_back_in_the_same_order(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        """The behaviour `_SORTS`' tiebreak exists for, asserted as behaviour.

        A dictionary that ends every clause in `t.id asc` is a claim about a
        dictionary. This is the claim about the board: five applications
        sharing one `last_signal_at`, which is what a backfill produces by the
        dozen, come back in one order and stay in it.
        """
        signal = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
        async with db.session(user_id) as connection:
            company = await connection.fetchval(
                """
                insert into companies (canonical_name) values ('Tied')
                on conflict (lower(canonical_name), coalesce(domain, '')) do update
                  set canonical_name = excluded.canonical_name
                returning id
                """
            )
            for n in range(5):
                await connection.execute(
                    """
                    insert into applications
                      (user_id, company_id, role_title, current_stage, current_phase,
                       confidence, last_signal_at)
                    values ($1,$2,$3,'applied','sent',1.0,$4)
                    """,
                    user_id,
                    company,
                    f"Engineer {n}",
                    signal,
                )

        for sort in _SORTS:
            reads = []
            for _ in range(3):
                body = (await client.get(f"/api/applications?sort={sort}")).json()
                reads.append([row["id"] for row in body["rows"]])
            assert len(reads[0]) == 5, sort
            assert reads[0] == reads[1] == reads[2], sort

    async def test_every_order_ends_on_the_id(self) -> None:
        # Without a tiebreak two applications with the same last signal — which
        # a backfill produces by the dozen — come back in whatever order the
        # plan happened to produce, so the board reshuffles between two reads
        # that asked the same question.
        #
        assert all(order.endswith("t.id asc") for order in _SORTS.values())

    async def test_the_cursor_is_absent_rather_than_wrong(
        self, client: AsyncClient
    ) -> None:
        # The reference paginated with `a.id > cursor` while ordering by
        # `last_signal_at desc, id asc`: a keyset predicate on a column that is
        # not the sort key, which skips rows and repeats others. Null is the
        # honest answer until a correct one is needed.
        body = (await client.get("/api/applications")).json()
        assert body["next_cursor"] is None
        # And an unknown query parameter is still ignored rather than a 422, so
        # a client that sends one is not broken by its absence.
        assert (await client.get("/api/applications?cursor=whatever")).status_code == 200


class TestServingTheBuiltClient:
    """The service worker precaches `/`, `/index.html` and the manifest with
    one `cache.addAll`. A single 404 there aborts installation entirely, so
    what this serves and how is not a detail.
    """

    @pytest.fixture
    def built(self, tmp_path: Path) -> Path:
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
        (tmp_path / "index.html").write_text("<!doctype html><title>Loop</title>")
        (tmp_path / "sw.js").write_text("self.addEventListener('install', () => {})")
        (tmp_path / "manifest.webmanifest").write_text("{}")
        # Nested, and not under `/assets`: the narrower version of this handler
        # served single-segment root files only, so this 404ed in production
        # and nowhere else.
        (tmp_path / "icons").mkdir()
        (tmp_path / "icons" / "apple-touch-icon.png").write_bytes(b"\x89PNG")
        return tmp_path

    @pytest.fixture
    async def serving(self, dsn: str, built: Path) -> AsyncClient:
        app = create_app(
            Settings(dsn=dsn, session_secret="test-secret", client_dir=built)
        )
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http

    async def test_serves_the_files_that_have_to_sit_at_the_root(
        self, serving: AsyncClient
    ) -> None:
        for path in ("/sw.js", "/manifest.webmanifest", "/index.html"):
            assert (await serving.get(path)).status_code == 200, path

    async def test_serves_a_nested_asset_that_is_not_under_assets(
        self, serving: AsyncClient
    ) -> None:
        assert (await serving.get("/icons/apple-touch-icon.png")).status_code == 200

    async def test_answers_head_as_well_as_get(self, serving: AsyncClient) -> None:
        # `@fastify/static` answered both. A service worker revalidating a
        # cached asset with HEAD would otherwise get a 405.
        assert (await serving.head("/sw.js")).status_code == 200

    async def test_the_root_is_the_app(self, serving: AsyncClient) -> None:
        response = await serving.get("/", headers={"accept": "text/html"})
        assert response.status_code == 200
        assert "<title>Loop</title>" in response.text

    async def test_a_route_the_single_page_app_owns_gets_the_shell(
        self, serving: AsyncClient
    ) -> None:
        response = await serving.get("/onboarding/scan", headers={"accept": "text/html"})
        assert response.status_code == 200
        assert "<title>Loop</title>" in response.text

    async def test_a_missing_file_is_still_a_404(self, serving: AsyncClient) -> None:
        assert (await serving.get("/nope.js")).status_code == 404

    async def test_an_api_path_never_becomes_a_file(self, serving: AsyncClient) -> None:
        # The catch-all is registered last, but "last" is an ordering rather
        # than a guarantee, and an unknown `/api` path must stay an API answer.
        response = await serving.get("/api/nothing-here")
        assert response.status_code in (401, 404)
        assert response.json()["error"]["code"] in ("unauthenticated", "not_found")

    async def test_cannot_be_walked_out_of_the_build_directory(
        self, serving: AsyncClient
    ) -> None:
        for attempt in ("/../pyproject.toml", "/..%2f..%2fetc%2fpasswd"):
            assert (await serving.get(attempt)).status_code in (404, 400), attempt


class TestTheSessionSecret:
    def test_is_required_rather_than_defaulted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # It had a default of `"dev-secret"`. Every CSRF token is
        # `HMAC(secret, "csrf:" + session_id)` and every OAuth `state` is signed
        # with the same key, so a deployment that forgot the variable derived
        # both from a string printed in the source.
        monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
        monkeypatch.setenv("LOOP_KEK", base64.b64encode(os.urandom(32)).decode())
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="SESSION_SECRET"):
            Settings.from_env()

    def test_an_empty_one_counts_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Compose writes `SESSION_SECRET=` for a value nobody has generated.
        monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
        monkeypatch.setenv("SESSION_SECRET", "   ")
        with pytest.raises(RuntimeError, match="SESSION_SECRET"):
            Settings.from_env()

    def test_and_is_taken_when_it_is_there(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
        monkeypatch.setenv("SESSION_SECRET", "a-real-secret")
        assert Settings.from_env().session_secret == "a-real-secret"
