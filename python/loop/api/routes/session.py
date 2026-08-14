"""Getting in, and knowing who you are.

The first screen an unauthenticated visitor sees calls `/api/auth/state`, and
the whole shell hangs on `/api/me` — every other query is gated on it and the
client feeds its `csrf` into every mutation it will ever make. Without these two
the PWA renders nothing at all, which is why they are the first thing ported.
"""

from typing import Any

from fastapi import APIRouter, Request, Response

from loop.api import auth
from loop.api.errors import ApiError
from loop.api.json import read_json

router = APIRouter(prefix="/api")

_MIN_PASSWORD = 8


@router.get("/auth/state")
async def auth_state(request: Request) -> dict[str, Any]:
    """Public. Always 200, always exactly these two booleans."""
    db = request.app.state.db
    user = await auth.sole_user(db)
    if user is None:
        return {"seeded": False, "has_passkey": False}
    user_id, _email = user
    return {"seeded": True, "has_passkey": await auth.has_passkey(db, user_id)}


@router.post("/auth/recover")
async def recover(request: Request, response: Response) -> dict[str, Any]:
    """The recovery code. Currently the only working way into the product."""
    body = await read_json(request)
    password = body.get("password")
    if not isinstance(password, str) or len(password) < _MIN_PASSWORD:
        raise ApiError(400, "bad_body", "password required", "password")

    db = request.app.state.db
    user = await auth.sole_user(db)
    if user is None:
        raise ApiError(404, "not_seeded", "no user")
    user_id, _email = user

    if not await auth.check_recovery_password(db, user_id, password):
        raise auth.password_error()
    await auth.mark_recovery_used(db, user_id)

    sessions: auth.Sessions = request.app.state.sessions
    token, session = await sessions.create(user_id)
    response.headers["set-cookie"] = auth.session_cookie(
        token, secure=request.app.state.settings.secure_cookies
    )
    # `enroll_passkey` is unconditional: having signed in with the recovery
    # code, you are always invited to add an authenticator.
    return {"ok": True, "csrf": sessions.csrf(session), "enroll_passkey": True}


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        await request.app.state.sessions.destroy(token)
    response.headers["set-cookie"] = auth.cleared_cookie()
    del session
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    """The route the entire shell hangs on.

    Key order is the users row and then the token, because that is what the
    reference's object spread produces and a fixture diff would notice.
    """
    session = auth.require(getattr(request.state, "session", None))
    async with request.app.state.db.untenanted() as connection:
        row = await connection.fetchrow(
            "select email, tz, locale, display_currency from users where id = $1",
            session.user_id,
        )
    if row is None:
        raise auth.unauthenticated()
    return {
        "email": row["email"],
        "tz": row["tz"],
        "locale": row["locale"],
        "display_currency": row["display_currency"],
        "csrf": request.app.state.sessions.csrf(session),
    }

