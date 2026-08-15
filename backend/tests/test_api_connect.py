"""What connecting a mailbox actually writes.

Three rows, not one. The port had kept only the Gmail account and dropped both
the calendar account and the consent record — silently, with no note anywhere
saying it was deliberate, which is the shape a porting omission has rather than
the shape a decision has.
"""

import base64
import os
import time
from typing import Any

import pytest
from httpx import AsyncClient

from loop.api.routes import mailboxes
from loop.db import Database
from loop.google.client import Tokens

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """A throwaway key. `store_mailbox` seals the refresh token for real here."""
    monkeypatch.setenv("LOOP_KEK", base64.b64encode(os.urandom(32)).decode())

GRANTED = (
    "https://www.googleapis.com/auth/gmail.readonly"
    " https://www.googleapis.com/auth/calendar.readonly"
)


class FakeGoogle:
    """Only the two calls the callback makes."""

    def __init__(self, scope: str = GRANTED) -> None:
        self._scope = scope

    async def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> Tokens:
        return Tokens(
            access_token="ya29.access",
            refresh_token="1//0grefresh",
            expires_in=3600,
            scope=self._scope,
        )

    async def profile(self, access_token: str) -> dict[str, Any]:
        return {"emailAddress": "you@example.com"}

    async def aclose(self) -> None:
        return None


async def connect(
    client: AsyncClient,
    user_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scope: str = GRANTED,
) -> Any:
    monkeypatch.setattr(mailboxes, "_client", lambda settings: FakeGoogle(scope))
    # `session_secret` is the app's; the test client and the app share one
    # process, so signing here is signing as the server.
    state = mailboxes._sign(
        {"u": user_id, "v": "verifier", "e": int(time.time()) + 600},
        "test-secret",
    )
    return await client.get(
        "/api/mailboxes/gmail/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )


class TestTheCallback:
    async def test_one_grant_is_one_row(
        self,
        client: AsyncClient,
        db: Database,
        user_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        response = await connect(client, user_id, monkeypatch)
        assert response.status_code == 303

        async with db.session(user_id) as connection:
            providers = [
                row["provider"]
                for row in await connection.fetch(
                    "select provider from mailbox_accounts where user_id = $1"
                    " order by provider",
                    user_id,
                )
            ]
        # A second `google_calendar` row was written here for a while, on the
        # reasoning that two cursors want two rows. Nothing read it:
        # `ConnectorService` selects `where provider = 'gmail'` and
        # `_sync_calendar` stores its sync token in that row's cursor beside
        # the history id. What the extra row did do was stay at
        # `last_ok_at = null` and `status = 'ok'` for ever, which made
        # `mailbox_health`'s freshness reading permanently null and the F1
        # revoked-access screen unreachable — and `DELETE /api/mailboxes/{id}`
        # removes one row by id, so disconnecting left a second sealed copy of
        # the refresh token behind.
        assert providers == ["gmail"]

    async def test_records_the_consent_with_the_scopes_that_were_granted(
        self,
        client: AsyncClient,
        db: Database,
        user_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await connect(client, user_id, monkeypatch)

        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                "select kind, version, detail from consents where user_id = $1", user_id
            )
        assert row is not None
        assert row["kind"] == "mailbox_scopes"
        assert row["version"] == mailboxes.SCOPE_VERSION
        # A dict, because `detail` is `jsonb` and the pool's codec encodes it.
        # This read `"gmail.readonly" in row["detail"]` and passed as a
        # *substring* test — which is what a doubly-encoded value decodes to,
        # so the assertion that was meant to prove the column was written
        # correctly was the one thing that could not fail when it was not.
        assert row["detail"] == {
            "scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
            ]
        }

    async def test_records_what_google_granted_not_what_was_asked_for(
        self,
        client: AsyncClient,
        db: Database,
        user_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A user may decline the calendar half at the consent screen. A record
        # that says what was requested answers a subject-access request with a
        # guess.
        partial = "https://www.googleapis.com/auth/gmail.readonly"
        await connect(client, user_id, monkeypatch, scope=partial)

        async with db.session(user_id) as connection:
            detail = await connection.fetchval(
                "select detail from consents where user_id = $1", user_id
            )
        # Against the list, not against the object: `in` on a dict asks about
        # keys, so `"calendar.readonly" not in detail` was true whether or not
        # the calendar scope had been granted.
        assert detail["scopes"] == [partial]

    async def test_the_gateway_role_may_write_all_three(
        self, client: AsyncClient, dsn: str, user_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The grants in migration 003 have never been exercised on this path:
        # the reference connected as a superuser, so an insert nobody holds
        # would have worked there and failed here.
        await connect(client, user_id, monkeypatch)
        async with (
            Database(dsn, role="loop_gateway") as gateway,
            gateway.session(user_id) as connection,
        ):
            assert (
                await connection.fetchval(
                    "select count(*) from consents where user_id = $1", user_id
                )
                == 1
            )
