"""Is this application still happening?

`status` cannot answer that on its own. It is `live` from the moment an
application is created until something — a rejection, the nightly sweep, a human
— moves it, so a mailbox that has been read for a year accumulates dozens of
`live` rows nobody has thought about since spring. Counting those as your
pipeline is the difference between "you have fourteen applications open" and the
truth, which was four.

Worse, it poisons every ratio on the statistics page: the display gates count
*closed* applications, so processes that are over but were never marked over
hold the denominator below the gate and the page shows an em dash next to a
funnel that plainly has numbers in it. That is the bug this module exists to
fix, and fixing it in one place is why the same three states are used by the
board, the counters and the metrics.

Three states, and the order of the rules is the whole of the definition:

    closed — over, whatever the column says. An explicit outcome, the sweep's
             `presumed_closed`, or silence long enough to mean it.
    stale  — quiet past the threshold for its stage, not yet long enough to
             write off. This is what a follow-up is for.
    active — moving, or waiting on you, or with a date in the calendar.

`loop/api/routes/activity_sql.py` asks the same question of a whole table, built
from these same constants; `tests/test_activity.py` pins the ladder.
"""

from datetime import datetime
from typing import Literal

from .thresholds import (
    NO_REPLY_CLOSED_DAYS,
    PRESUMED_CLOSED_DAYS,
    PRESUMED_CLOSED_SKIP_STAGES,
)

Activity = Literal["active", "stale", "closed"]

_SECONDS_PER_DAY = 86_400.0
# What the dormancy sweep falls back to when a stage names no staleness.
_DEFAULT_STALE_AFTER_DAYS = 21.0


def closure_days(current_phase: str) -> int:
    """How long silence has to run before it is a decision rather than a delay.

    Shorter for an application still in the `sent` phase — applied, or
    acknowledged by a robot and nothing since. There is no panel to convene and
    no calendar to fit; two months of that is a no that nobody typed.
    """
    return NO_REPLY_CLOSED_DAYS if current_phase == "sent" else PRESUMED_CLOSED_DAYS


def activity_of(
    *,
    now: datetime,
    status: str,
    current_stage: str,
    current_phase: str,
    presumed_closed: bool = False,
    last_signal_at: datetime | None = None,
    next_interview_at: datetime | None = None,
    quiet_threshold_days: float | None = None,
) -> Activity:
    # An outcome that was actually recorded outranks every inference below it.
    if status != "live":
        return "closed"

    # A date in the calendar is the strongest evidence there is that something
    # is still happening, and it outranks silence: an interview booked six weeks
    # out leaves a long quiet gap that means the opposite of what quiet means.
    if next_interview_at is not None and next_interview_at > now:
        return "active"

    if presumed_closed:
        return "closed"

    # The ball is in your court. Silence here is a task, not a verdict — the
    # same exemption the dormancy sweep makes, for the same reason.
    if current_stage in PRESUMED_CLOSED_SKIP_STAGES:
        return "active"

    # Nothing has ever arrived on this row. A manual add on the day it is made
    # reads as active rather than as silent, which is what it is.
    if last_signal_at is None:
        return "active"

    quiet = (now - last_signal_at).total_seconds() / _SECONDS_PER_DAY
    if quiet > closure_days(current_phase):
        return "closed"

    threshold = (
        _DEFAULT_STALE_AFTER_DAYS if quiet_threshold_days is None else quiet_threshold_days
    )
    return "stale" if quiet > threshold else "active"


def is_open(activity: Activity) -> bool:
    """Open is what the board shows by default: everything that is not over."""
    return activity != "closed"
