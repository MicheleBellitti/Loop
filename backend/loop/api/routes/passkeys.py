"""Signing in with something that is not a password.

Four routes and two table columns. The browser asks for options, the
authenticator signs the challenge, and the server checks the signature against a
public key it stored at registration — so there is no shared secret, nothing to
phish and nothing to leak in a dump.

**The challenge read is fixed here, and that changes behaviour.** The reference
consumed a challenge with

    update auth_secrets set webauthn_challenge = null
     where user_id = $1 and challenge_expires_at > now()
     returning webauthn_challenge

and `returning` on an update yields the *new* row. On Postgres 16 — which is
what this runs on — that is unconditionally null, so both verify routes answered
`no_challenge` every time, having already burned the challenge. No passkey could
ever be enrolled and no passkey login could ever succeed; the recovery code was
the only way into the product, and the code read as though that were a choice.

The one-shot semantics are what matters and they are kept: a CTE reads the row
and the update consumes it in the same statement, so a replayed assertion finds
nothing.
"""

import base64
import logging
import secrets
from typing import Any, Final

from fastapi import APIRouter, Depends, Request, Response

from loop.api import auth
from loop.api.errors import ApiError
from loop.api.json import read_json
from loop.api.ratelimit import LOGIN, limit

router = APIRouter(prefix="/api")

_log = logging.getLogger("loop.api.passkeys")

# Long enough to reach for a phone, short enough that a captured challenge is
# worthless by the time anyone has it.
CHALLENGE_TTL = "5 minutes"
_TIMEOUT_MS: Final = 60_000

# ES256, EdDSA and RS256 — the three every authenticator in circulation
# implements between them.
_ALGORITHMS = (-8, -7, -257)

# One-shot, and readable on Postgres 16. The CTE sees the row as it was; the
# update empties it in the same statement.
#
# **A column each, and that is not tidiness.** Enrolling shares nothing with
# signing in but a table: `register/options` needs a session and `login/options`
# is public by necessity, so while the two wrote one slot an unauthenticated
# request could replace the challenge an enrolment was waiting on — and the
# ordinary way to hit it is a second tab left on the sign-in screen, not an
# attacker. Migration 018.
_TAKE_LOGIN_CHALLENGE = """
with held as (
  select webauthn_challenge from auth_secrets
   where user_id = $1 and challenge_expires_at > now()
)
update auth_secrets set webauthn_challenge = null
 where user_id = $1 and challenge_expires_at > now()
 returning (select webauthn_challenge from held)
"""

_SAVE_LOGIN_CHALLENGE = f"""
insert into auth_secrets (user_id, recovery_hash, webauthn_challenge, challenge_expires_at)
values ($1, '', $2, now() + interval '{CHALLENGE_TTL}')
on conflict (user_id) do update
  set webauthn_challenge = excluded.webauthn_challenge,
      challenge_expires_at = excluded.challenge_expires_at
"""

_TAKE_REGISTRATION_CHALLENGE = """
with held as (
  select registration_challenge from auth_secrets
   where user_id = $1 and registration_expires_at > now()
)
update auth_secrets set registration_challenge = null
 where user_id = $1 and registration_expires_at > now()
 returning (select registration_challenge from held)
"""

_SAVE_REGISTRATION_CHALLENGE = f"""
insert into auth_secrets
  (user_id, recovery_hash, registration_challenge, registration_expires_at)
values ($1, '', $2, now() + interval '{CHALLENGE_TTL}')
on conflict (user_id) do update
  set registration_challenge = excluded.registration_challenge,
      registration_expires_at = excluded.registration_expires_at
"""


@router.post("/auth/register/options")
async def register_options(request: Request) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    settings = request.app.state.settings
    db = request.app.state.db

    async with db.untenanted() as connection:
        email = await connection.fetchval(
            "select email from users where id = $1", session.user_id
        )
        existing = await connection.fetch(
            "select credential_id from credentials where user_id = $1", session.user_id
        )
        challenge = _random()
        await connection.execute(_SAVE_REGISTRATION_CHALLENGE, session.user_id, challenge)

    return {
        "challenge": challenge,
        "rp": {"name": settings.webauthn.rp_name, "id": settings.webauthn.rp_id},
        "user": {
            # A fresh handle each time, never the row's id. The user id is a
            # tenant key and an authenticator is a device that may be shared or
            # backed up — the two should not be the same value.
            "id": _random(),
            "name": email or "loop",
            "displayName": "",
        },
        "pubKeyCredParams": [{"alg": alg, "type": "public-key"} for alg in _ALGORITHMS],
        "timeout": _TIMEOUT_MS,
        "attestation": "none",
        # So an authenticator already enrolled says so rather than silently
        # registering a second credential for the same key.
        "excludeCredentials": [
            {"id": row["credential_id"], "type": "public-key"} for row in existing
        ],
        "authenticatorSelection": {
            "residentKey": "preferred",
            "userVerification": "required",
            "requireResidentKey": False,
        },
        "extensions": {"credProps": True},
        "hints": [],
    }


@router.post("/auth/register/verify")
async def register_verify(request: Request) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    settings = request.app.state.settings
    body = await read_json(request)

    async with request.app.state.db.untenanted() as connection:
        challenge = await connection.fetchval(
            _TAKE_REGISTRATION_CHALLENGE, session.user_id
        )
        if not challenge:
            raise ApiError(400, "no_challenge", "request options first")

        credential = _verified_registration(body, challenge, settings)
        await connection.execute(
            """
            insert into credentials
              (user_id, credential_id, public_key, counter, transports, label)
            values ($1,$2,$3,$4,$5,'passkey')
            on conflict (credential_id) do nothing
            """,
            session.user_id,
            credential["id"],
            credential["public_key"],
            credential["counter"],
            credential["transports"],
        )
    return {"ok": True}


@router.post("/auth/login/options", dependencies=[Depends(limit(LOGIN))])
async def login_options(request: Request) -> dict[str, Any]:
    """Public: this is the screen you see before you are anybody."""
    settings = request.app.state.settings
    db = request.app.state.db

    user = await auth.sole_user(db)
    if user is None:
        raise ApiError(404, "not_seeded", "run the seed script")
    user_id, _email = user

    async with db.untenanted() as connection:
        credentials = await connection.fetch(
            "select credential_id, transports from credentials where user_id = $1", user_id
        )
        challenge = _random()
        await connection.execute(_SAVE_LOGIN_CHALLENGE, user_id, challenge)

    return {
        "rpId": settings.webauthn.rp_id,
        "challenge": challenge,
        # Always present, possibly empty: the client branches on its length to
        # decide whether to offer the passkey button at all.
        "allowCredentials": [
            {
                "id": row["credential_id"],
                "transports": list(row["transports"] or ()),
                "type": "public-key",
            }
            for row in credentials
        ],
        "timeout": _TIMEOUT_MS,
        "userVerification": "required",
    }


@router.post("/auth/login/verify", dependencies=[Depends(limit(LOGIN))])
async def login_verify(request: Request, response: Response) -> dict[str, Any]:
    settings = request.app.state.settings
    db = request.app.state.db
    body = await read_json(request)

    user = await auth.sole_user(db)
    if user is None:
        raise ApiError(404, "not_seeded", "no user")
    user_id, _email = user

    async with db.untenanted() as connection:
        stored = await connection.fetchrow(
            """
            select credential_id, public_key, counter from credentials
             where user_id = $1 and credential_id = $2
            """,
            user_id,
            body.get("id"),
        )
        # Looked up before the challenge is consumed, unlike the reference: an
        # assertion for an unknown credential should not burn the challenge a
        # real authenticator is about to answer.
        if stored is None:
            raise ApiError(401, "unknown_credential", "unknown passkey")

        challenge = await connection.fetchval(_TAKE_LOGIN_CHALLENGE, user_id)
        if not challenge:
            raise ApiError(400, "no_challenge", "request options first")

        counter = _verified_assertion(body, challenge, stored, settings)
        await connection.execute(
            """
            update credentials set counter = $3, last_used_at = now()
             where user_id = $1 and credential_id = $2
            """,
            user_id,
            stored["credential_id"],
            counter,
        )

    sessions: auth.Sessions = request.app.state.sessions
    token, established = await sessions.create(user_id)
    response.headers["set-cookie"] = auth.session_cookie(
        token, secure=settings.secure_cookies
    )
    return {"ok": True, "csrf": sessions.csrf(established)}


@router.get("/auth/session-check")
async def session_check(request: Request) -> dict[str, Any]:
    """Whether the cookie in hand is still worth anything.

    The one route that answers the same way to everyone, so the client can ask
    without handling a 401 — which is why it is not behind the gate.
    """
    session = await request.app.state.sessions.load(
        request.cookies.get(auth.COOKIE_NAME)
    )
    return {"authenticated": session is not None}


def _verified_registration(
    body: dict[str, Any], challenge: str, settings: Any
) -> dict[str, Any]:
    from webauthn import verify_registration_response
    from webauthn.helpers.exceptions import InvalidRegistrationResponse

    try:
        verified = verify_registration_response(
            credential=body,
            expected_challenge=_bytes(challenge),
            expected_origin=settings.public_origin,
            expected_rp_id=settings.webauthn.rp_id,
            require_user_verification=True,
        )
    except (InvalidRegistrationResponse, ValueError, KeyError) as error:
        # The library's own sentence stays in the log and not in the response:
        # it describes what the authenticator sent, and a browser has no use
        # for it.
        _log.warning("registration rejected: %s", error)
        raise ApiError(400, "bad_attestation", "verification failed") from error

    return {
        "id": _b64(verified.credential_id),
        "public_key": verified.credential_public_key,
        "counter": verified.sign_count,
        "transports": [str(t) for t in (body.get("response", {}).get("transports") or ())],
    }


def _verified_assertion(
    body: dict[str, Any], challenge: str, stored: Any, settings: Any
) -> int:
    from webauthn import verify_authentication_response
    from webauthn.helpers.exceptions import InvalidAuthenticationResponse

    try:
        verified = verify_authentication_response(
            credential=body,
            expected_challenge=_bytes(challenge),
            expected_origin=settings.public_origin,
            expected_rp_id=settings.webauthn.rp_id,
            credential_public_key=bytes(stored["public_key"]),
            credential_current_sign_count=int(stored["counter"]),
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, ValueError, KeyError) as error:
        _log.warning("assertion rejected: %s", error)
        raise ApiError(401, "bad_assertion", "verification failed") from error
    return int(verified.new_sign_count)


def _random() -> str:
    """Thirty-two bytes, as the forty-three characters the browser expects."""
    return _b64(secrets.token_bytes(32))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _bytes(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
