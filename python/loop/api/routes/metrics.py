"""Thirteen numbers, in the one format everything already scrapes.

Hand-rolled, and the reason is worth stating: a Prometheus client library brings
a registry, a multiprocess mode, a WSGI app and a set of default collectors, to
serialise thirty lines of text. The exposition format is a documented text
format and this is a text formatter.

What is measured is the same thirteen the reference registers, but where the
reference's gateway served an empty registry — the counters live in the worker
processes and nothing ever incremented one here — these are read from the
database on request. The queue depths and the dead-letter count are facts about
Postgres, and asking Postgres is both simpler than aggregating counters across
processes and correct after a restart.
"""

from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from loop.db import Queue, dead_letter_depth

router = APIRouter()

# Prometheus text exposition, version 0.0.4. No charset: the format's own media
# type carries the encoding.
CONTENT_TYPE: Final = "text/plain; version=0.0.4"

_SECONDS_PER_HOUR: Final = 3600


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> PlainTextResponse:
    """Public, like every other scrape endpoint, and it says nothing personal.

    Counts and ages, no ids, no addresses, no company names. That is what makes
    leaving it open defensible rather than an oversight — but it is still a
    statement about how much mail this mailbox gets, so it belongs behind the
    same network boundary as the database.
    """
    lines: list[str] = []
    async with request.app.state.db.untenanted() as connection:
        depths = {}
        for queue in Queue.ALL:
            row = await connection.fetchrow("select * from mq.metrics($1)", queue)
            depths[queue] = int(row["queue_length"] or 0) if row else 0
        dead = await dead_letter_depth(connection)
        review = await connection.fetchval(
            "select count(*) from review_items where resolved_at is null"
        )
        freshness = await connection.fetchval(
            "select extract(epoch from now() - max(last_ok_at)) from mailbox_accounts"
        )

    _series(
        lines,
        "queue_depth",
        "gauge",
        "Messages waiting per queue",
        [({"queue": queue}, depth) for queue, depth in depths.items()],
    )
    _series(lines, "dead_letters", "gauge", "Messages in a dead-letter queue", [({}, dead)])
    _series(lines, "review_items_open", "gauge", "Open review items", [({}, int(review or 0))])
    if freshness is not None:
        _series(
            lines,
            "mailbox_freshness_seconds",
            "gauge",
            "Seconds since a mailbox last read successfully",
            [({}, float(freshness))],
        )
    # The content type is set as a header rather than through `media_type`,
    # which appends a charset to anything under `text/` — and Prometheus's
    # media type carries its version parameter there instead.
    return PlainTextResponse(
        "\n".join(lines) + "\n", headers={"content-type": CONTENT_TYPE}
    )


def _series(
    lines: list[str],
    name: str,
    kind: str,
    help_text: str,
    samples: list[tuple[dict[str, str], Any]],
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")
    for labels, value in samples:
        lines.append(f"{name}{_labels(labels)} {value}")


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    pairs = ",".join(f'{key}="{_escaped(labels[key])}"' for key in sorted(labels))
    return f"{{{pairs}}}"


def _escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
