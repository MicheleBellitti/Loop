"""The statistics page, and the honesty rules that make it worth reading.

Every ratio here travels with its own numerator, denominator and exclusion
count, because a conversion rate without its denominator is a number you cannot
argue with. Below the gate the value is null and the note says what unlocks it,
so an empty chart reads as a progress bar rather than as a disappointment.

Three of the ten sections ignore `period` entirely — channels, time in stage and
compensation are whole-history views with no window to apply. That is the
reference's shape and it is defensible: a channel's effectiveness over ninety
days is a sample of about four applications.
"""

from typing import Any, Final, Literal

import asyncpg
from fastapi import APIRouter, Request

from loop.api import auth
from loop.db import load_stage_table
from loop.domain import (
    Metric,
    channel_gate,
    dwell_metric,
    format_percent,
    ratio,
    seasonal_gate,
)
from loop.domain.metrics import format_days, format_money

router = APIRouter(prefix="/api")

Period = Literal["90d", "12m", "all"]
_DEFAULT_PERIOD: Final[Period] = "12m"

# Interpolated, not bound: an interval cannot be a parameter. Safe because the
# key is one of exactly three literals and anything else has already become the
# default before it reaches here.
_WINDOW: dict[str, str] = {
    "90d": "and coalesce(a.applied_at, a.created_at) > now() - interval '90 days'",
    "12m": "and coalesce(a.applied_at, a.created_at) > now() - interval '12 months'",
    "all": "",
}
_REACH_WINDOW: dict[str, str] = {
    "90d": "and coalesce(r.applied_at, r.created_at) > now() - interval '90 days'",
    "12m": "and coalesce(r.applied_at, r.created_at) > now() - interval '12 months'",
    "all": "",
}

_CHANNEL_NOTE = (
    "Referrals are reported separately on purpose — folding them into LinkedIn "
    "would flatter it."
)

# Too recent to have converted yet. Counting these in the denominator would make
# every ratio drift downwards on a week you applied a lot, which is exactly
# backwards.
_IMMATURE_DAYS = 21


def _funnel_sql(window: str) -> str:
    """Five counts in a fixed order, and the order is the contract.

    The first two branches read `applications` directly and the last two read a
    materialised view the pipeline refreshes on a five-second debounce, so the
    top of this funnel is live and the bottom can lag. Inherited, and worth
    knowing when the numbers look briefly impossible.
    """
    return f"""
    select 'Applied' as label, count(*) as n from applications a
     where a.user_id = $1 and a.merged_into_id is null {window}
    union all
    select 'Acknowledged', count(*) from applications a
     where a.user_id = $1 and a.merged_into_id is null {window}
       and exists (select 1 from application_events e
                    where e.application_id = a.id and e.type = 'acknowledged')
    union all
    select 'Screening', count(*) from applications a
     where a.user_id = $1 and a.merged_into_id is null {window}
       and exists (select 1 from application_events e
                     join stage_defs sd on sd.user_id = a.user_id and sd.key = e.to_stage
                    where e.application_id = a.id
                      and sd.phase in ('screening','interviewing','decided'))
    union all
    select 'Interviewing', count(*) from applications a
      join app_phase_reach r on r.id = a.id
     where a.user_id = $1 and a.merged_into_id is null {window} and r.reached_interview
    union all
    select 'Offer', count(*) from applications a
      join app_phase_reach r on r.id = a.id
     where a.user_id = $1 and a.merged_into_id is null {window} and r.reached_offer
    """


def _conversion_sql(window: str) -> str:
    return f"""
    with cohort as (
      select r.*, a.current_phase from app_phase_reach r
      join applications a on a.id = r.id
      where r.user_id = $1 {window}
    ), judged as (
      select *, (status = 'live' and not reached_interview
                 and applied_at > now() - interval '{_IMMATURE_DAYS} days') as immature
        from cohort
    )
    select count(*) filter (where reached_interview and not immature) as numerator,
           count(*) filter (where not immature) as denominator,
           count(*) filter (where immature) as excluded,
           count(*) filter (where status <> 'live') as closed
      from judged
    """


def _offers_sql(window: str) -> str:
    return f"""
    select count(*) filter (where reached_offer) as numerator,
           count(*) filter (where reached_interview and status <> 'live') as denominator,
           count(*) filter (where status <> 'live') as closed
      from app_phase_reach r
     where r.user_id = $1 {window}
    """


def _timing_sql(window: str) -> str:
    return f"""
    select percentile_cont(0.5) within group (
             order by extract(epoch from (first_human_at - applied_at)) / 86400
           ) as median_days,
           count(*) filter (where first_human_at is not null) as n
      from app_phase_reach r
     where r.user_id = $1 and applied_at is not null {window}
    """


def _ghosted_sql(window: str) -> str:
    return f"""
    select count(*) filter (where status = 'dormant') as ghosted,
           count(*) filter (where status <> 'live') as closed
      from app_phase_reach r
     where r.user_id = $1 {window}
    """


# The three views below carry no row-level security of their own — a plain view
# resolves its base tables as the owner — so the `user_id` predicate in each is
# the whole of the isolation, not a belt over a policy's braces.
_CHANNELS = """
select channel, sent, interviews, offers, ghosted
  from channel_effectiveness where user_id = $1 order by sent desc
"""

_DWELL = """
select stage, p50_days, n from stage_dwell_in where user_id = $1 order by stage
"""

_COMP = "select kind, min_minor, max_minor, currency from comp_offers where user_id = $1"

_QUARTERS = """
select count(distinct date_trunc('quarter', applied_at)) as n
  from applications where user_id = $1 and applied_at is not null
"""


@router.get("/stats")
async def stats(request: Request, period: str = _DEFAULT_PERIOD) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    async with request.app.state.db.session(session.user_id) as connection:
        return await stats_payload(connection, session.user_id, period)


async def stats_payload(
    connection: asyncpg.Connection, user_id: str, period: str
) -> dict[str, Any]:
    """The whole statistics response, from an open tenant session.

    Public because it is the page *and* the chat assistant's view of the same
    figures — one definition, so the two can never disagree about a ratio.
    """
    window = period if period in _WINDOW else _DEFAULT_PERIOD

    stages = await load_stage_table(connection, user_id)
    currency = await connection.fetchval(
        "select display_currency from users where id = $1", user_id
    )
    funnel = await connection.fetch(_funnel_sql(_WINDOW[window]), user_id)
    conversion = await connection.fetchrow(
        _conversion_sql(_REACH_WINDOW[window]), user_id
    )
    offers = await connection.fetchrow(_offers_sql(_REACH_WINDOW[window]), user_id)
    timing = await connection.fetchrow(_timing_sql(_REACH_WINDOW[window]), user_id)
    ghosted = await connection.fetchrow(_ghosted_sql(_REACH_WINDOW[window]), user_id)
    channels = await connection.fetch(_CHANNELS, user_id)
    dwell = await connection.fetch(_DWELL, user_id)
    comp = await connection.fetch(_COMP, user_id)
    quarters = await connection.fetchval(_QUARTERS, user_id)

    to_interview = ratio(
        numerator=conversion["numerator"],
        denominator=conversion["denominator"],
        closed=conversion["closed"],
        excluded=conversion["excluded"],
        exclusion_reason="too recent to count",
    )
    to_offer = ratio(
        numerator=offers["numerator"],
        denominator=offers["denominator"],
        closed=offers["closed"],
    )
    ghost = ratio(
        numerator=ghosted["ghosted"],
        denominator=ghosted["closed"],
        closed=ghosted["closed"],
    )
    seasonal_met, seasonal_note = seasonal_gate(int(quarters or 0))
    median_days = timing["median_days"]

    return {
        "period": window,
        "funnel": _funnel(funnel),
        "ratios": [
            {"label": "Application → interview", **_metric(to_interview)},
            {"label": "Interview → offer", **_metric(to_offer)},
        ],
        "first_response": {
            "value": float(median_days) if median_days is not None else None,
            "n": timing["n"],
            "display": format_days(float(median_days)) if median_days is not None else "—",
            "caption": "median to first human reply",
        },
        "ghost": {
            **_metric(ghost),
            "caption": "closed by silence, not by a no",
        },
        "channels": [_channel(row) for row in channels],
        "channel_note": _CHANNEL_NOTE,
        "time_in_stage": [_stage(row, stages) for row in dwell],
        "compensation": _compensation(comp, currency or "EUR"),
        # The reference emitted the "needs two quarters" sentence even once it
        # had them, from a constant rather than from its own gate. The gate is
        # in the domain, it counts down the remaining quarters, and the mobile
        # view already only shows the note when the gate is unmet.
        "seasonal": {"gate_met": seasonal_met, "note": seasonal_note},
    }


def _funnel(rows: Any) -> list[dict[str, Any]]:
    counts = [{"label": row["label"], "n": row["n"]} for row in rows]
    top = counts[0]["n"] if counts else 0
    return [
        {**entry, "width": round(entry["n"] / top * 100) if top else 0} for entry in counts
    ]


def _metric(metric: Metric) -> dict[str, Any]:
    """A metric plus the string the client prints, in the reference's key order."""
    return {
        "value": metric.value,
        "numerator": metric.numerator,
        "denominator": metric.denominator,
        "excluded": metric.excluded,
        "gate_met": metric.gate_met,
        "note": metric.note,
        "small_sample": metric.small_sample,
        "display": format_percent(metric.value),
    }


def _channel(row: Any) -> dict[str, Any]:
    sent = row["sent"]
    gate_met, note = channel_gate(sent)

    def rate(count: int) -> str:
        # Strings, not numbers: below the gate there is no honest figure and an
        # em dash is the answer, so the column has one type either way.
        return format_percent(count / sent) if gate_met and sent else "—"

    return {
        "name": row["channel"],
        "sent": sent,
        "gate_met": gate_met,
        "iv": rate(row["interviews"]),
        # Offers over *applications sent*, not over interviews — it sits beside
        # `iv` and does not mean what its neighbour means.
        "of": rate(row["offers"]),
        "ghost": rate(row["ghosted"]),
        "note": note,
    }


def _stage(row: Any, stages: Any) -> dict[str, Any]:
    metric = dwell_metric(float(row["p50_days"]), row["n"])
    return {
        "stage": stages.label_of(row["stage"]),
        "days": float(row["p50_days"]),
        "n": row["n"],
        "gate_met": metric.gate_met,
    }


def _compensation(rows: Any, display_currency: str) -> dict[str, Any]:
    """One scale under three tracks: what was posted, what you asked, what came.

    Rows in a currency this cannot convert are counted and dropped rather than
    folded in at some rate nobody chose. `dropped` is what says so.
    """
    usable = [row for row in rows if row["currency"] == display_currency]
    dropped = len(rows) - len(usable)

    amounts: list[int] = [
        int(row[column])
        for row in usable
        for column in ("min_minor", "max_minor")
        if row[column] is not None
    ]
    if not amounts:
        return {
            "domain": None,
            "posted": [],
            "ask": None,
            "offers": [],
            "currency": display_currency,
            "dropped": dropped,
        }

    low, high = min(amounts), max(amounts)
    span = (high - low) or 1

    def at(value: int) -> int:
        return round((value - low) / span * 100)

    ask = next((row for row in usable if row["kind"] == "ask"), None)
    return {
        "domain": {"min": low, "max": high, "currency": display_currency},
        "posted": [
            {
                "from": at(row["min_minor"]),
                "to": at(row["max_minor"] or row["min_minor"]),
                "label": (
                    f"{format_money(row['min_minor'], row['currency'])}–"
                    f"{format_money(row['max_minor'] or row['min_minor'], row['currency'])}"
                ),
            }
            for row in usable
            if row["kind"] == "posted_range"
        ],
        "ask": {"at": at(ask["min_minor"])} if ask else None,
        "offers": [
            {
                "at": at(row["min_minor"]),
                "label": format_money(row["min_minor"], row["currency"]),
            }
            for row in usable
            if row["kind"] == "offer"
        ],
        "currency": display_currency,
        "dropped": dropped,
    }
