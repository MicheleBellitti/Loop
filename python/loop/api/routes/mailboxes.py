"""Connecting a mailbox, disconnecting one, and being told there is new mail.

The OAuth dance is the one place a user hands this system something irreversible,
so the two halves that protect it are worth naming.

**The state parameter is signed, not stored.** It carries the user it belongs to
and an expiry, under an HMAC of the session secret, so the callback can verify
who started the flow without a table and without a row that outlives the
redirect. A callback that arrives with a state this server did not mint is
refused before a code is exchanged.

**PKCE is not optional here.** The verifier never leaves the server and the
challenge is what Google echoes back, so an intercepted authorisation code is
worth nothing on its own.

The push webhook is deliberately thin. It reads no body at all — the connector
re-reads history from its own cursor — so the worst a forged notification can do
is cause one extra sync.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from loop.api import auth
from loop.api.errors import ApiError
from loop.api.json import read_json
from loop.api.mailbox import mailbox_health
from loop.google.client import GoogleAuthError, GoogleClient
from loop.google.crypto import Sealed, open_sealed, unwrap_dek
from loop.google.mailbox import NoRefreshToken, store_mailbox

router = APIRouter(prefix="/api")

_log = logging.getLogger("loop.api.mailboxes")

# Long enough to read a consent screen, short enough that a link left in a chat
# is worthless tomorrow.
_STATE_TTL_SECONDS: Final = 600

_MAX_BACKFILL_MONTHS: Final = 60

@router.get("/mailboxes")
async def list_mailboxes(request: Request) -> dict[str, Any]:
    """The health of the connection, not a list of rows.

    The name says list and the answer is a status, which reads oddly until you
    see what asks: the shell polls this to decide whether to full-screen "access
    revoked" and whether to send a new user to onboarding. It wants one object
    with a `state` and a `providers` array, and it is the same object
    `/api/today` carries — so it is the same function.

    Returning a `{"mailboxes": [...]}` list here instead, which is what a route
    called `GET /api/mailboxes` looks like it should do, blanks the entire app:
    `App.tsx` reads `health.providers.length` before it renders anything.
    """
    session = auth.require(getattr(request.state, "session", None))
    async with request.app.state.db.session(session.user_id) as connection:
        return await mailbox_health(connection, session.user_id)


@router.post("/mailboxes/gmail/start")
async def start(request: Request) -> dict[str, Any]:
    """Where to send the browser, and the verifier kept behind the cookie.

    The verifier rides in the signed state rather than in a table: it is
    single-use, it expires in ten minutes, and a row that has to be cleaned up
    after a user abandons a consent screen is a row that will not be.
    """
    session = auth.require(getattr(request.state, "session", None))
    settings = request.app.state.settings
    if not settings.google.configured:
        raise ApiError(503, "not_configured", "no Google client is configured")

    verifier = _random()
    challenge = _challenge(verifier)
    state = _sign(
        {"u": session.user_id, "v": verifier, "e": int(time.time()) + _STATE_TTL_SECONDS},
        settings.session_secret,
    )
    return {
        "url": GoogleClient.authorisation_url(
            client_id=settings.google.client_id or "",
            redirect_uri=_redirect_uri(settings),
            code_challenge=challenge,
            state=state,
        )
    }


@router.get("/mailboxes/gmail/callback")
async def callback(request: Request, code: str = "", state: str = "") -> Response:
    """Where Google sends the browser back. Always a redirect, never JSON.

    A user is looking at this, so every outcome is a page rather than a status
    code — and the failure reason travels as a short slug rather than as
    Google's own words, which can carry the request back into a URL bar.
    """
    settings = request.app.state.settings
    claims = _verify(state, settings.session_secret)
    if claims is None or not code:
        return RedirectResponse("/onboarding?connected=0&reason=state", status_code=303)

    client = _client(settings)
    try:
        tokens = await client.exchange_code(code, _redirect_uri(settings), claims["v"])
        profile = await client.profile(tokens.access_token)
        async with request.app.state.db.session(claims["u"]) as connection:
            await store_mailbox(
                connection,
                user_id=claims["u"],
                provider="gmail",
                address=profile["emailAddress"],
                tokens=tokens,
            )
    except NoRefreshToken:
        # Google withholds the refresh token when a grant already exists.
        # Storing what it did send would overwrite a working mailbox with a
        # secret that cannot be refreshed.
        _log.warning("Google returned no refresh token; the mailbox is unchanged")
        return RedirectResponse("/onboarding?connected=0&reason=no_refresh", status_code=303)
    except GoogleAuthError as error:
        _log.warning("could not complete the Google connection: %s", error)
        return RedirectResponse("/onboarding?connected=0&reason=denied", status_code=303)
    finally:
        await client.aclose()

    return RedirectResponse("/onboarding?connected=1", status_code=303)


@router.post("/mailboxes/backfill")
async def backfill(request: Request) -> dict[str, Any]:
    """How far back to read, which is the user's decision and only theirs."""
    session = auth.require(getattr(request.state, "session", None))
    body = await read_json(request)
    months = body.get("months")
    if not isinstance(months, int) or not 1 <= months <= _MAX_BACKFILL_MONTHS:
        raise ApiError(
            400, "bad_body", f"between 1 and {_MAX_BACKFILL_MONTHS} months", "months"
        )
    mailbox_id = body.get("mailbox_id")

    async with request.app.state.db.session(session.user_id) as connection:
        if mailbox_id is None:
            mailbox_id = await connection.fetchval(
                "select id from mailbox_accounts where user_id = $1"
                " order by created_at limit 1",
                session.user_id,
            )
        if mailbox_id is None:
            raise ApiError(404, "not_found", "no mailbox is connected")

    # The connector holds the credentials and the rate budget; the gateway only
    # says when. A notification rather than a queue message because a backfill
    # request is not worth replaying if the connector was down — the user will
    # ask again, and a duplicate scan costs Google quota for nothing.
    async with request.app.state.db.untenanted() as connection:
        await connection.execute(
            "select pg_notify('loop_backfill', $1)",
            json.dumps({"mailbox_id": str(mailbox_id), "months": months}),
        )
    return {"ok": True, "months": months}


@router.delete("/mailboxes/{mailbox_id}")
async def disconnect(request: Request, mailbox_id: str) -> dict[str, Any]:
    """Remove the account, and tell Google we are done with the grant.

    The revoke is fire-and-forget and the delete is not conditional on it: a
    user asking to disconnect must not be left connected because Google was
    briefly unreachable.
    """
    session = auth.require(getattr(request.state, "session", None))
    settings = request.app.state.settings

    async with request.app.state.db.session(session.user_id) as connection:
        row = await connection.fetchrow(
            """
            select id, secret_ciphertext, secret_nonce, dek_wrapped, dek_nonce
              from mailbox_accounts where id = $1 and user_id = $2
            """,
            mailbox_id,
            session.user_id,
        )
        if row is None:
            raise ApiError(404, "not_found", "no such mailbox")
        await connection.execute("delete from mailbox_accounts where id = $1", mailbox_id)

    if settings.google.configured:
        client = _client(settings)
        try:
            await client.revoke(_refresh_token(row))
        finally:
            await client.aclose()
    # Everything the mailbox produced goes with it: `seen_messages` cascades on
    # the foreign key, so a reconnected mailbox re-reads from scratch.
    return {"ok": True}


@router.post("/gmail/push")
async def pubsub(request: Request) -> Response:
    """Google says there is new mail. It does not say what, and nor does this.

    The body is never read. The connector re-reads history from its own stored
    cursor, so a forged notification can at most cause one extra sync — which is
    what makes it safe for this to be a public route, and why the notification
    carries no user, no history id and no address.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return Response(status_code=401)
    if not await _google_signed(authorization[7:], request.app.state.settings):
        _log.warning("rejected an unsigned push notification")
        return Response(status_code=401)

    async with request.app.state.db.untenanted() as connection:
        await connection.execute("select pg_notify('loop_connector', 'push')")
    return Response(status_code=204)


async def _google_signed(token: str, settings: Any) -> bool:
    """Verify the Pub/Sub bearer token against Google's keys.

    Deliberately stricter than the reference in two places. The audience must be
    exactly the configured origin — the reference did a substring test on the
    host, which a token for any audience containing that string would pass — and
    a non-URL audience raised out of the check and became a 500 instead of a
    401.
    """
    try:
        from jwt import PyJWKClient, decode
    except ModuleNotFoundError:
        _log.error("pyjwt is not installed; the push webhook cannot verify anything")
        return False

    try:
        key = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs").get_signing_key_from_jwt(
            token
        )
        decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=settings.public_origin,
            issuer=["https://accounts.google.com", "accounts.google.com"],
        )
    except Exception:
        return False
    return True


def _client(settings: Any) -> GoogleClient:
    return GoogleClient(
        client_id=settings.google.client_id or "",
        client_secret=settings.google.client_secret or "",
    )


def _redirect_uri(settings: Any) -> str:
    return f"{settings.public_origin.rstrip('/')}/api/mailboxes/gmail/callback"


def _refresh_token(row: Any) -> str:
    dek = unwrap_dek(Sealed(bytes(row["dek_wrapped"]), bytes(row["dek_nonce"])))
    opened = open_sealed(
        Sealed(bytes(row["secret_ciphertext"]), bytes(row["secret_nonce"])), dek
    )
    return str(json.loads(opened).get("refresh_token", ""))


def _random() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _sign(claims: dict[str, Any], secret: str) -> str:
    """`<payload>.<mac>`, both base64url. Nothing is stored anywhere."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


def _verify(state: str, secret: str) -> dict[str, Any] | None:
    payload, _, presented = state.partition(".")
    if not payload or not presented:
        return None
    expected = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    if not hmac.compare_digest(presented, expected):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if not isinstance(claims, dict) or claims.get("e", 0) < time.time():
        return None
    return claims
