"""Is it still reading my mail?

The one question that matters, and the reason it is a route rather than a log
line: a connector that has quietly stopped is indistinguishable from a quiet job
market, and the difference is a month of your life.

`ok` is deliberately narrow — dead letters and the oldest unprocessed message,
nothing else. Staleness and the component list are reported and do not vote,
because a mailbox that has been quiet for six hours at 3 a.m. is not a fault and
a health check that cries wolf gets muted.
"""

from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Request

from loop.db import Queue, dead_letter_depth
from loop.domain.thresholds import OLDEST_UNPROCESSED_ALERT_MINUTES

router = APIRouter()

_STALE_SECONDS: Final = OLDEST_UNPROCESSED_ALERT_MINUTES * 60
_MODEL_TIMEOUT_SECONDS: Final = 2.0
_SECONDS_PER_HOUR: Final = 3600


@router.get("/health/deep")
async def deep(request: Request) -> dict[str, Any]:
    """Public, and polled by the dashboard every sixty seconds.

    Everything below is one round trip per queue plus one model probe, and it
    runs unauthenticated on purpose: a health check you need a session for is a
    health check that cannot tell you why you cannot sign in.
    """
    db = request.app.state.db
    depths: dict[str, int] = {}
    oldest = 0

    async with db.untenanted() as connection:
        for queue in Queue.ALL:
            row = await connection.fetchrow("select * from mq.metrics($1)", queue)
            depths[queue] = int(row["queue_length"] or 0) if row else 0
            oldest = max(oldest, int(row["oldest_msg_age_sec"] or 0) if row else 0)
        dead = await dead_letter_depth(connection)
        # No tenant here, deliberately: this is a question about the box, not
        # about one user's mailbox — and this connection is the owner's, which
        # is the only way that query sees anything at all.
        last_ok = await connection.fetchval(
            "select max(last_ok_at) from mailbox_accounts"
        )

    return {
        "ok": dead == 0 and oldest < _STALE_SECONDS,
        "queues": depths,
        "oldest_unprocessed_seconds": oldest,
        "dead_letters": dead,
        "mailbox_staleness_hours": _hours_since(last_ok),
        "components": {
            # Two literals and one live probe, which is what the reference
            # reports. Naming them here rather than pretending to check them
            # keeps the one that can actually fail legible.
            "template_rules": "running",
            "calendar_detection": "running",
            "local_model": await _model_state(request),
        },
    }


def _hours_since(moment: datetime | None) -> float | None:
    if moment is None:
        return None
    return (datetime.now(UTC) - moment).total_seconds() / _SECONDS_PER_HOUR


async def _model_state(request: Request) -> str:
    base_url = request.app.state.settings.model_base_url
    if not base_url:
        return "disabled"

    import httpx

    try:
        async with httpx.AsyncClient(timeout=_MODEL_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{base_url.rstrip('/')}/models")
    except httpx.HTTPError:
        return "unreachable"
    return "reachable" if response.is_success else f"http {response.status_code}"
