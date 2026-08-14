"""Row flags and quiet counters.

The client "never computes a statistic, never derives a stage, and never
decides whether an application is dormant — all of that arrives precomputed,
including `days_quiet` and each row's `flag`". The prototypes show three
different flag strings on the same column and never say what happens when two
apply at once, so the precedence is defined here: soonest irreversible cost
first. decisions.md C2.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from .thresholds import DEADLINE_FLAG_WINDOW_DAYS
from .types import AppStatus

# "Last signal" turns accent past this many days on the desktop table.
LAST_SIGNAL_EMPHASIS_DAYS = 13

FlagKind = Literal["deadline", "decide", "quiet", "none"]

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True, slots=True)
class Flag:
    kind: FlagKind
    text: str


def days_quiet(now: datetime, last_signal_at: datetime | None) -> int | None:
    if last_signal_at is None:
        return None
    return max(0, int((now - last_signal_at).total_seconds() // 86_400))


def quiet_label(days: int | None) -> str:
    """ "1 day" / "6 days" — the pipeline row's meta line."""
    if days is None:
        return ""
    return "quiet 1 day" if days == 1 else f"quiet {days} days"


def _weekday_time(d: datetime, tz: str) -> str:
    local = d.astimezone(ZoneInfo(tz))
    return f"{_WEEKDAYS[local.weekday()]} {local:%H:%M}"


def _day_month(d: datetime, tz: str) -> str:
    local = d.astimezone(ZoneInfo(tz))
    return f"{local.day} {_MONTHS[local.month - 1]}"


def compute_flag(
    *,
    now: datetime,
    tz: str,
    status: AppStatus,
    deadline_at: datetime | None = None,
    decide_by: datetime | None = None,
    last_signal_at: datetime | None = None,
    quiet_threshold_days: float | None = None,
) -> Flag:
    """One value per row, first match wins.

    1. a take-home deadline inside a week — the only cost you cannot undo
    2. an offer you owe an answer to
    3. silence past your own p90 for this stage

    A closed application never carries a flag: it has nothing left to be late
    for.
    """
    if status in ("rejected", "withdrawn", "accepted"):
        return Flag("none", "")

    if deadline_at is not None:
        hours = (deadline_at - now).total_seconds() / 3_600
        if 0 < hours <= DEADLINE_FLAG_WINDOW_DAYS * 24:
            return Flag("deadline", f"Due {_weekday_time(deadline_at, tz)}")

    if decide_by is not None and decide_by > now:
        return Flag("decide", f"decide by {_day_month(decide_by, tz)}")

    quiet = days_quiet(now, last_signal_at)
    if status == "dormant" or (
        quiet is not None and quiet_threshold_days is not None and quiet > quiet_threshold_days
    ):
        return Flag("quiet", "quiet · past your p90")

    return Flag("none", "")
