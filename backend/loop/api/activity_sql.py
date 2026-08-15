"""`activity_of`, as one SQL expression.

The same ladder as `loop.domain.activity`, in the same order, over a whole table
— the numbers come from the same constants so the two cannot drift apart on a
threshold change, and the domain module carries the argument for each rung.

It is SQL and not a filter applied to the rows afterwards because the board, the
counters and every ratio ask this question with a `where` on it, and a filter
applied after `limit` would quietly hand back a short page and call it the whole
pipeline.
"""

from typing import Final

from loop.domain.thresholds import (
    NO_REPLY_CLOSED_DAYS,
    PRESUMED_CLOSED_DAYS,
    PRESUMED_CLOSED_SKIP_STAGES,
)

_SKIP = ",".join(f"'{stage}'" for stage in sorted(PRESUMED_CLOSED_SKIP_STAGES))

# Reads `sd` (stage_defs), `d` (stage_dwell_in) and `nx` (the next interview)
# from whichever select embeds it; `JOINS` below is that boilerplate.
ACTIVITY: Final = f"""
  case
    when a.status <> 'live' then 'closed'
    when nx.starts_at is not null then 'active'
    when a.presumed_closed then 'closed'
    when a.current_stage in ({_SKIP}) then 'active'
    when a.last_signal_at is null then 'active'
    when a.last_signal_at < now() - make_interval(days => case when a.current_phase = 'sent'
           then {NO_REPLY_CLOSED_DAYS} else {PRESUMED_CLOSED_DAYS} end) then 'closed'
    when a.last_signal_at < now() - make_interval(days => ceil(greatest(
           coalesce(sd.stale_after_days, 21), coalesce(2 * d.p90_days, 0)))::int) then 'stale'
    else 'active'
  end"""

JOINS: Final = """
  left join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
  left join stage_dwell_in d on d.user_id = a.user_id and d.stage = a.current_stage and d.n >= 5
  left join lateral (
    select min(i.starts_at) as starts_at from interviews i
     where i.application_id = a.id and i.cancelled_at is null and i.starts_at > now()
  ) nx on true"""

# One user's applications with their activity, for the statistics to join on.
# `$1` is the user id, as everywhere else in this package.
CTE: Final = f"""act as (
  select a.id, a.status, a.current_stage, a.current_phase, {ACTIVITY} as activity
    from applications a {JOINS}
   where a.user_id = $1 and a.merged_into_id is null
)"""

# Closed without anybody ever saying no. That is what a ghost rate measures, and
# `status = 'dormant'` alone misses every application the sweep has not reached.
GHOSTED: Final = "act.activity = 'closed' and act.status in ('live','dormant')"

# What each `activity` filter admits. `open` is the default and it is the
# product's answer to "what am I actually doing": everything not written off,
# quiet ones included, because a quiet application is the one that most needs
# you. History is a deliberate second request rather than the thing you wade
# through to find today's work.
FILTERS: Final[dict[str, tuple[str, ...] | None]] = {
    "open": ("active", "stale"),
    "active": ("active",),
    "stale": ("stale",),
    "closed": ("closed",),
    "all": None,
}
DEFAULT_FILTER: Final = "open"


def filter_sql(activity: str | None) -> str:
    """The `where` fragment for a filter name, or nothing at all for `all`."""
    wanted = FILTERS.get(activity or DEFAULT_FILTER, FILTERS[DEFAULT_FILTER])
    if wanted is None:
        return ""
    return " and t.activity in (" + ",".join(f"'{state}'" for state in wanted) + ")"
