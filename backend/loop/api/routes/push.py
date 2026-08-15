"""The browser's end of the notification path.

Two routes and one row. The public VAPID key goes out so the browser can build a
subscription, and the subscription comes back so the notifier has somewhere to
send. Everything that decides *whether* to send lives in the notifier; nothing
here is a policy.
"""

from typing import Any

from fastapi import APIRouter, Request

from loop.api import auth
from loop.api.errors import ApiError
from loop.api.json import read_json

router = APIRouter(prefix="/api")


@router.get("/push/key")
async def public_key(request: Request) -> dict[str, Any]:
    """Null rather than an error when there are no keys.

    A deployment without VAPID keys is a product without notifications, not a
    broken one, and onboarding branches on the null instead of on a status code.
    """
    return {"public_key": request.app.state.settings.vapid.public_key or None}


@router.post("/push/subscribe")
async def subscribe(request: Request) -> dict[str, Any]:
    """One row per device, and re-subscribing rotates the keys in place.

    A browser regenerates its subscription whenever it feels like it — after a
    permission change, after a service-worker update — and the endpoint is the
    identity, so the upsert keeps the row it already had rather than growing a
    second one for the same phone.
    """
    session = auth.require(getattr(request.state, "session", None))
    body = await read_json(request)
    endpoint = body.get("endpoint")
    keys = body.get("keys")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ApiError(400, "bad_body", "a push endpoint", "endpoint")
    if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        raise ApiError(400, "bad_body", "the subscription keys", "keys")

    async with request.app.state.db.session(session.user_id) as connection:
        await connection.execute(
            """
            insert into push_subscriptions (user_id, endpoint, p256dh, auth)
            values ($1,$2,$3,$4)
            on conflict (user_id, endpoint) do update
              set p256dh = excluded.p256dh, auth = excluded.auth
            """,
            session.user_id,
            endpoint,
            str(keys["p256dh"]),
            str(keys["auth"]),
        )
    return {"ok": True}
