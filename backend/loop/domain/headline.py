"""The Today headline.

The handoff is emphatic that this "is generated from the week's events — it is
the product's one encouraging gesture and must never be a static string", and
that when the week had no forward movement it must fall back to a neutral
statement of fact, "never to false cheer". It then gives no generation rule, so
here is one. decisions.md C1.

Four outcomes, matching the four headlines drawn across the prototypes:

    forward movement    → "Three moved / forward / this week"
    nothing tracked yet → "Nothing / to track yet"              (empty state E1)
    nothing needs you   → "You are / clear today"               (empty state E2)
    otherwise           → "Nine applications / waiting"

The third case is the one worth defending: a pipeline that is full and quiet is
a good day, and saying so is not cheer — it is the accurate reading.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from .stages import DEFAULT_STAGES, StageTable
from .types import DomainEvent

HeadlineKind = Literal["moved", "empty", "clear", "waiting"]

_WORDS = (
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
)

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def number_word(n: int) -> str:
    """Small numbers read better as words; past twelve, the numeral is clearer."""
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


@dataclass(frozen=True, slots=True)
class Headline:
    lines: tuple[str, ...]
    # Which branch produced it — the client uses it for nothing, tests use it.
    kind: HeadlineKind
    # Applications that moved forward in the window.
    moved_count: int


def is_forward_event(ev: DomainEvent, stages: StageTable | None = None) -> bool:
    """Movement that counts as forward.

    A stage change that goes *backwards* — an extra round was added — is
    legitimate and common, and reporting it as progress would be exactly the
    false cheer the design forbids.
    """
    table = stages or DEFAULT_STAGES
    if ev.type in ("interview_scheduled", "offer_received"):
        return True
    if ev.type == "stage_advanced":
        to = ev.to_stage or ev.payload.get("to_stage")
        if not isinstance(to, str):
            return False
        return table.is_forward(ev.from_stage, to)
    return False


def build_headline(
    *,
    events: Iterable[DomainEvent],
    application_id_of: Callable[[DomainEvent], str],
    live_count: int,
    open_suggestion_count: int,
    now: datetime,
    window_days: int = 7,
    stages: StageTable | None = None,
) -> Headline:
    since = now - timedelta(days=window_days)

    moved: set[str] = set()
    for ev in events:
        if ev.occurred_at < since or ev.occurred_at > now:
            continue
        if is_forward_event(ev, stages):
            moved.add(application_id_of(ev))

    n = len(moved)
    if n > 0:
        return Headline((f"{number_word(n)} moved", "forward", "this week"), "moved", n)
    if live_count == 0:
        return Headline(("Nothing", "to track yet"), "empty", 0)
    if open_suggestion_count == 0:
        return Headline(("You are", "clear today"), "clear", 0)
    return Headline((f"{number_word(live_count)} applications", "waiting"), "waiting", 0)


def date_eyebrow(now: datetime, tz: str) -> str:
    """ "Thursday 30 July" — computed server-side, in the user's timezone."""
    local = now.astimezone(ZoneInfo(tz))
    return f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}"
