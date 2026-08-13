"""Passkeys, and the one statement that made them impossible.

The interesting test here is the challenge. The reference consumed one with an
`update … returning` of the column it had just nulled, which on Postgres 16
returns the new value — so every verify answered `no_challenge` having already
burned the challenge, and nobody could ever enrol or use a passkey.

The signature checking itself belongs to the library and is not re-tested here.
What is tested is everything around it: who may ask, what is stored, and that a
challenge works exactly once.
"""

import asyncpg
import pytest
from httpx import AsyncClient

from loop.db import Database

pytestmark = pytest.mark.integration


class TestAskingForOptions:
    async def test_registration_needs_a_session(self, anonymous: AsyncClient) -> None:
        assert (await anonymous.post("/api/auth/register/options")).status_code == 401

    async def test_and_returns_what_the_browser_asks_for(
        self, client: AsyncClient
    ) -> None:
        token = (await client.get("/api/me")).json()["csrf"]
        body = (
            await client.post(
                "/api/auth/register/options", headers={"x-csrf-token": token}
            )
        ).json()

        assert set(body) == {
            "challenge",
            "rp",
            "user",
            "pubKeyCredParams",
            "timeout",
            "attestation",
            "excludeCredentials",
            "authenticatorSelection",
            "extensions",
            "hints",
        }
        assert len(body["challenge"]) == 43
        # A fresh handle, never the row's id: a tenant key and a device
        # identifier should not be the same value.
        assert body["user"]["id"] != body["challenge"]
        assert body["authenticatorSelection"]["userVerification"] == "required"

    async def test_signing_in_is_public_and_says_what_is_enrolled(
        self, anonymous: AsyncClient
    ) -> None:
        body = (await anonymous.post("/api/auth/login/options")).json()
        assert set(body) == {
            "rpId",
            "challenge",
            "allowCredentials",
            "timeout",
            "userVerification",
        }
        # Present and possibly empty: the client branches on its length to
        # decide whether to offer the passkey button at all.
        assert isinstance(body["allowCredentials"], list)


class TestTheChallenge:
    async def test_it_is_stored_and_can_be_spent_once(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        token = (await client.get("/api/me")).json()["csrf"]
        issued = (
            await client.post(
                "/api/auth/register/options", headers={"x-csrf-token": token}
            )
        ).json()["challenge"]

        async with db.untenanted() as connection:
            stored = await connection.fetchval(
                "select webauthn_challenge from auth_secrets where user_id = $1", user_id
            )
            assert stored == issued

            # The read the reference got wrong: this must hand back the value it
            # is consuming, not the null it leaves behind.
            from loop.api.routes.passkeys import _TAKE_CHALLENGE

            taken = await connection.fetchval(_TAKE_CHALLENGE, user_id)
            assert taken == issued

            spent_again = await connection.fetchval(_TAKE_CHALLENGE, user_id)
            assert spent_again is None

    async def test_verifying_without_one_says_so(self, client: AsyncClient) -> None:
        token = (await client.get("/api/me")).json()["csrf"]
        response = await client.post(
            "/api/auth/register/verify", json={}, headers={"x-csrf-token": token}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "no_challenge"


class TestSigningIn:
    async def test_an_unknown_passkey_does_not_burn_the_challenge(
        self, anonymous: AsyncClient, db: Database
    ) -> None:
        issued = (await anonymous.post("/api/auth/login/options")).json()["challenge"]

        response = await anonymous.post("/api/auth/login/verify", json={"id": "nope"})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unknown_credential"
        # The reference consumed the challenge before looking the credential up,
        # so a stray request invalidated the one a real authenticator was about
        # to answer.
        async with db.untenanted() as connection:
            still_there = await connection.fetchval(
                "select webauthn_challenge from auth_secrets where user_id = $1",
                await _sole_user(connection),
            )
        assert still_there == issued


class TestSessionCheck:
    async def test_it_answers_everyone(self, anonymous: AsyncClient) -> None:
        assert (await anonymous.get("/api/auth/session-check")).json() == {
            "authenticated": False
        }

    async def test_and_says_yes_with_a_cookie(self, client: AsyncClient) -> None:
        assert (await client.get("/api/auth/session-check")).json() == {
            "authenticated": True
        }


async def _sole_user(connection: asyncpg.Connection) -> str:
    """Whoever `/api/auth/login/options` was about: the oldest account."""
    return str(await connection.fetchval("select id from users order by created_at limit 1"))
