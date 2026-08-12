"""The four nudge rules, as a pure function of a snapshot.

"A scheduled fold over the log, not a chat. Each rule produces at most one
suggestion per application, expires on its own, and is written so that doing
nothing is always acceptable."

The service around this decides *when* to run and *whether* to push; this
decides *what is true*. Keeping it pure is what makes the budget testable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from .clock import days_between, hours_between, relative_future, relative_past
from .stages import DEFAULT_STAGES, StageTable
from .thresholds import (
    DEADLINE_SUGGESTION_WINDOW_DAYS,
    FOLLOW_UP_EXPIRY_DAYS,
    LET_IT_GO_AFTER_DORMANT_DAYS,
    MAX_OPEN_SUGGESTIONS,
    PREPARE_WINDOW_HOURS,
)
from .types import AppStatus

NudgeRule = Literal["deadline", "prepare", "follow_up_due", "let_it_go"]

# Urgency order for the "ranked by urgency then depth" rule (Spec §12).
_RULE_URGENCY: dict[NudgeRule, int] = {
    "deadline": 0,
    "prepare": 1,
    "follow_up_due": 2,
    "let_it_go": 3,
}

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True, slots=True)
class AppSnapshot:
    id: str
    company: str
    role_title: str | None
    current_stage: str
    status: AppStatus
    last_signal_at: datetime | None
    # True when the ball is in their court — set by the pipeline from the log.
    awaiting_them: bool
    # When the user last acted on this application (archive, correct, note).
    last_user_action_at: datetime | None
    went_dormant_at: datetime | None


@dataclass(frozen=True, slots=True)
class InterviewSnapshot:
    id: str
    application_id: str
    stage: str
    starts_at: datetime


@dataclass(frozen=True, slots=True)
class DeadlineSnapshot:
    application_id: str
    kind: str
    due_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class Suggestion:
    # `rule:application_id` — stable, so re-running does not duplicate.
    key: str
    rule: NudgeRule
    application_ids: tuple[str, ...]
    kind: str
    meta: str
    title: str
    body: str
    cta: str
    # When this becomes moot without any user action.
    expires_at: datetime | None
    # What the ranking sorts on: sooner is more urgent.
    urgency_at: datetime
    depth: int
    # Whether the rule is allowed to produce a push at all (Spec §12).
    pushable: bool
    # Only the deadline rule may ignore the cap and the quiet window.
    bypasses_budget: bool


@dataclass(slots=True)
class NudgeInput:
    now: datetime
    applications: Sequence[AppSnapshot]
    interviews: Sequence[InterviewSnapshot] = ()
    deadlines: Sequence[DeadlineSnapshot] = ()
    # p75 dwell for a stage from the user's own history, None below the gate.
    p75_dwell_days: Callable[[str], float | None] = lambda _s: None
    # p50, used only for the copy ("your median wait here is 3 days").
    p50_dwell_days: Callable[[str], float | None] = lambda _s: None
    # Keys of suggestions already issued and not yet expired. One per
    # application per rule, ever, unless it expired and re-triggered.
    open_or_issued: frozenset[str] = field(default_factory=frozenset)
    stages: StageTable | None = None


def suggestion_key(rule: NudgeRule, application_id: str) -> str:
    return f"{rule}:{application_id}"


def _fmt_list(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])} and {names[-1]}"


def evaluate_nudges(inp: NudgeInput) -> list[Suggestion]:
    """Every suggestion that is currently true, unranked and unbudgeted."""
    stages = inp.stages or DEFAULT_STAGES
    now = inp.now
    out: list[Suggestion] = []
    by_id = {a.id: a for a in inp.applications}

    def is_open(key: str) -> bool:
        return key in inp.open_or_issued

    # ── deadline ────────────────────────────────────────────────────────────
    # The only hard alert in the system, because it is the only one where
    # silence has a cost you cannot undo.
    for d in inp.deadlines:
        app = by_id.get(d.application_id)
        if not app or app.status != "live":
            continue
        hours = hours_between(now, d.due_at)
        if hours <= 0 or hours > DEADLINE_SUGGESTION_WINDOW_DAYS * 24:
            continue
        key = suggestion_key("deadline", app.id)
        if is_open(key):
            continue
        out.append(
            Suggestion(
                key=key,
                rule="deadline",
                application_ids=(app.id,),
                kind="deadline",
                meta=relative_future(now, d.due_at),
                title=f"{app.company} {d.kind.replace('_', '-')} due "
                f"{_WEEKDAYS[d.due_at.weekday()]}",
                body=f"Parsed from the {d.source} email. This is the only alert "
                "allowed to interrupt you.",
                cta="Open brief",
                expires_at=d.due_at,
                urgency_at=d.due_at,
                depth=stages.depth_of(app.current_stage),
                pushable=True,
                bypasses_budget=True,
            )
        )

    # ── prepare ─────────────────────────────────────────────────────────────
    # No advice generation — just everything you already wrote, in one place,
    # at the right hour.
    for iv in inp.interviews:
        app = by_id.get(iv.application_id)
        if not app or app.status != "live":
            continue
        hours = hours_between(now, iv.starts_at)
        if hours <= 0 or hours > PREPARE_WINDOW_HOURS:
            continue
        key = suggestion_key("prepare", app.id)
        if is_open(key):
            continue
        when = relative_future(now, iv.starts_at)
        out.append(
            Suggestion(
                key=key,
                rule="prepare",
                application_ids=(app.id,),
                kind="prepare",
                meta=when,
                title=f"{app.company} {stages.label_of(iv.stage).lower()} {when}",
                body="The posting, the thread, and everything you have already "
                "written about this company, in one place.",
                cta="Open the brief",
                expires_at=iv.starts_at,
                urgency_at=iv.starts_at,
                depth=stages.depth_of(app.current_stage),
                pushable=True,
                bypasses_budget=False,
            )
        )

    # ── follow_up_due ───────────────────────────────────────────────────────
    # Dwell past p75 of the user's *own* history for that stage. The fallback
    # exists because a new user has no history and would otherwise never be
    # nudged at all.
    for app in inp.applications:
        if app.status != "live" or not app.awaiting_them or not app.last_signal_at:
            continue
        dwell = days_between(app.last_signal_at, now)
        p75 = inp.p75_dwell_days(app.current_stage)
        threshold = p75 if p75 is not None else stages.stale_after_days(app.current_stage) * 0.6
        if dwell <= threshold:
            continue
        key = suggestion_key("follow_up_due", app.id)
        if is_open(key):
            continue
        p50 = inp.p50_dwell_days(app.current_stage)
        label = stages.label_of(app.current_stage).lower()
        body = (
            f"Nothing has come back in {dwell} days. A short nudge is normal here."
            if p50 is None
            else f"You last heard from them {dwell} days ago and your median wait "
            f"at this stage is {p50:g} days. A short nudge is normal here."
        )
        out.append(
            Suggestion(
                key=key,
                rule="follow_up_due",
                application_ids=(app.id,),
                kind="follow-up due",
                meta=relative_past(now, app.last_signal_at),
                title=f"{app.company} has gone quiet since the {label}",
                body=body,
                cta="Draft follow-up",
                expires_at=now + timedelta(days=FOLLOW_UP_EXPIRY_DAYS),
                urgency_at=app.last_signal_at + timedelta(days=threshold),
                depth=stages.depth_of(app.current_stage),
                pushable=True,
                bypasses_budget=False,
            )
        )

    # ── let_it_go ───────────────────────────────────────────────────────────
    # Batched into one card on purpose: this is the rule that keeps the
    # pipeline honest without making you admit defeat one card at a time.
    # Never pushed.
    lettable = [
        app
        for app in inp.applications
        if app.status == "dormant"
        and app.went_dormant_at is not None
        and days_between(app.went_dormant_at, now) >= LET_IT_GO_AFTER_DORMANT_DAYS
        and not (app.last_user_action_at and app.last_user_action_at > app.went_dormant_at)
        and not is_open(suggestion_key("let_it_go", app.id))
    ]
    if lettable:
        ids = tuple(a.id for a in lettable)
        oldest = min(lettable, key=lambda a: a.went_dormant_at or now)
        plural = "" if len(ids) == 1 else "s"
        out.append(
            Suggestion(
                key=suggestion_key("let_it_go", "+".join(ids)),
                rule="let_it_go",
                application_ids=ids,
                kind="let it go",
                meta=f"{len(ids)} application{plural}",
                title=f"{_fmt_list([a.company for a in lettable])} "
                f"look{'s' if len(lettable) == 1 else ''} finished",
                body="Silent past twice your usual wait. Archiving keeps your ratios "
                "honest — they stay in the statistics as ghosted.",
                cta="Archive it" if len(ids) == 1 else "Archive both",
                expires_at=None,
                urgency_at=oldest.went_dormant_at or now,
                depth=max(stages.depth_of(a.current_stage) for a in lettable),
                pushable=False,
                bypasses_budget=False,
            )
        )

    return out


def rank_and_cap(
    suggestions: Iterable[Suggestion],
    max_open: int = MAX_OPEN_SUGGESTIONS,
) -> list[Suggestion]:
    """At most three, ranked by urgency then depth."""
    return sorted(
        suggestions,
        key=lambda s: (_RULE_URGENCY[s.rule], s.urgency_at, -s.depth, s.key),
    )[:max_open]
