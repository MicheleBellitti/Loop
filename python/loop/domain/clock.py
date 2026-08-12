"""Timezone arithmetic.

Two jobs in this system are wall-clock jobs, not interval jobs: the 03:00
dormancy sweep and the 21:00–08:00 quiet window. Both are anchored to the
user's timezone from `users.tz` — a setting, not the device — because a
device-derived zone would move the dormancy job every time you travel.
decisions.md D4.

The TypeScript version had to reconstruct all of this from `Intl.DateTimeFormat`
parts and solve for the offset by iteration, because JavaScript has no timezone
type. `zoneinfo` is in the standard library, so most of that file disappears
here — which is one small, concrete example of the port being a simplification
rather than a translation.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

DAY = timedelta(days=1)


def zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz)


def parse_hhmm(hhmm: str) -> tuple[int, int]:
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time of day: {hhmm}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"invalid time of day: {hhmm}")
    return h, m


def at_local_time(now: datetime, tz: str, hhmm: str, day_offset: int = 0) -> datetime:
    """Today's wall-clock `HH:MM` in `tz`, as an instant.

    A spring-forward hour that does not exist locally resolves to the instant
    just after the jump, which is the behaviour a scheduled job wants: it fires
    once, late, rather than not at all.
    """
    h, m = parse_hhmm(hhmm)
    local = now.astimezone(zone(tz)) + timedelta(days=day_offset)
    naive = local.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=None)
    candidate = naive.replace(tzinfo=zone(tz))
    # `fold` disambiguates the autumn repeat; a nonexistent spring time is
    # normalised by round-tripping through UTC.
    return candidate.astimezone(UTC).astimezone(zone(tz))


def parse_quiet_hours(spec: str) -> tuple[str, str]:
    """`"21:00-08:00"` → `("21:00", "08:00")`."""
    parts = [p.strip() for p in spec.strip().split("-")]
    if len(parts) != 2:
        raise ValueError(f"invalid quiet hours: {spec}")
    parse_hhmm(parts[0])
    parse_hhmm(parts[1])
    return parts[0], parts[1]


def is_quiet_hour(now: datetime, tz: str, quiet: tuple[str, str]) -> bool:
    """True inside the window, which wraps past midnight."""
    local = now.astimezone(zone(tz))
    minutes = local.hour * 60 + local.minute
    fh, fm = parse_hhmm(quiet[0])
    th, tm = parse_hhmm(quiet[1])
    start, end = fh * 60 + fm, th * 60 + tm
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def next_deliverable_at(now: datetime, tz: str, quiet: tuple[str, str]) -> datetime:
    """The first instant at or after `now` when a notification may be delivered."""
    if not is_quiet_hour(now, tz, quiet):
        return now
    same_day = at_local_time(now, tz, quiet[1])
    if same_day > now:
        return same_day
    return at_local_time(now, tz, quiet[1], day_offset=1)


def days_between(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 86_400)


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3_600


def relative_future(now: datetime, then: datetime) -> str:
    """ "in 3 days", "in 4 hours", "tomorrow" — the meta line on a suggestion."""
    h = hours_between(now, then)
    if h < 0:
        return "overdue"
    if h < 1:
        return "within the hour"
    if h < 24:
        return f"in {round(h)} hours"
    d = round(h / 24)
    return "tomorrow" if d == 1 else f"in {d} days"


def relative_past(now: datetime, then: datetime) -> str:
    """ "6 days quiet" — the meta line on a follow-up card."""
    d = days_between(then, now)
    if d <= 0:
        return "today"
    return "1 day quiet" if d == 1 else f"{d} days quiet"
