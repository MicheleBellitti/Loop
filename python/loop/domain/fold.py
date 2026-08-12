"""The fold: a pure function from an event set to the state of one application.

No database, no clock, no randomness. `applications` is a projection that can
be dropped and rebuilt from this alone.

── Why this is not the rule in Engineering Spec §05 ────────────────────────

§05 says "take the event with the highest confidence; ties break by latest
occurred_at". That rule cannot work, and the bundle's own worked example proves
it: the Architecture sheet walks an `acknowledged` at confidence 0.99 (an ATS
auto-reply, day 0) followed eleven days later by `stage_advanced → hr_screen`
at 0.94. Highest-confidence-wins keeps the application at `acknowledged`
forever. Since virtually every auto-reply is a 0.99 template match and
virtually every human signal scores lower, the literal rule makes the pipeline
structurally unable to advance. The Architecture sheet §06 in fact states the
intent correctly: "the highest-confidence *latest* event".

So: recency decides, a human pins, and confidence is a gate plus a tie-break.

── Why `id` is not the final tie-break ─────────────────────────────────────

§05 ends ties with "highest id". `id` is a serial, i.e. arrival order — so
using it would make the fold depend on the order messages happened to be
delivered in, and the property test that replaying in any order yields the same
state could not pass. The tie-break here is content-derived (`evidence_ref` +
type), which is stable across replays. decisions.md A1.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from .stages import DEFAULT_STAGES, StageTable
from .thresholds import FOLD_CONFIDENCE_FLOOR, PINNED_CONFIDENCE
from .types import (
    TERMINAL_STATUSES,
    ApplicationState,
    AppStatus,
    Channel,
    CorrectableField,
    DomainEvent,
    EventType,
    WorkMode,
)

T = TypeVar("T")

_ORDINARY_TIER = 1
_TERMINAL_TIER = 2
_PINNED_TIER = 3


def _is_correction_for(ev: DomainEvent, field_name: CorrectableField) -> bool:
    """What "pinned" means.

    §05 says a human correction carries confidence 1.0 and pins its field, and
    it is tempting to read that as "confidence 1.0 pins". It cannot mean that:
    quick add also writes at 1.0 (you typed it, so it is certain), and if that
    pinned the stage then every hand-added application would be frozen at
    `applied` forever, deaf to its own mailbox.

    So pinning is about authorship and scope, not about the number: only a
    `human_corrected` event pins, and only the one field it names.
    """
    return (
        ev.type == "human_corrected"
        and ev.payload.get("field") == field_name
        and ev.confidence >= PINNED_CONFIDENCE
    )


def _is_terminal_event(ev: DomainEvent) -> bool:
    return ev.type in TERMINAL_STATUSES


def _tie_key(ev: DomainEvent) -> str:
    """Stable across replays, unlike a serial id."""
    return f"{ev.evidence_ref or ''} {ev.type}"


@dataclass(slots=True)
class _Candidate:
    value: Any
    ev: DomainEvent
    tier: int


def _sort_key(c: _Candidate) -> tuple[int, float, float, str]:
    return (c.tier, c.ev.occurred_at.timestamp(), c.ev.confidence, _tie_key(c.ev))


def _pick(cands: list[_Candidate]) -> _Candidate | None:
    """The winner under (pinned, occurred_at, confidence, tie-break).

    `max` with an explicit key rather than a comparator: the ordering is a
    total order on content, so the same set always yields the same winner
    regardless of the order it was assembled in. That is the property the
    determinism test asserts.
    """
    if not cands:
        return None
    return max(cands, key=_sort_key)


# Events that describe something the outside world did. They move
# `last_signal_at`, which is what "quiet N days" and the dormancy job read.
#
# `applied` counts even when it came from quick add: otherwise an application
# you typed in by hand would be flagged as quiet the moment you created it.
# `note_added`, `human_corrected` and `went_silent` do not count — the first two
# are you talking to yourself, and the third would reset the very clock that
# produced it.
INBOUND_SIGNALS: frozenset[EventType] = frozenset(
    {
        "applied",
        "acknowledged",
        "stage_advanced",
        "interview_scheduled",
        "interview_held",
        "deadline_set",
        "offer_received",
        "offer_negotiated",
        "rejected",
    }
)


def _implied_stage(ev: DomainEvent) -> str | None:
    """The stage each event type implies, when it implies one at all."""
    match ev.type:
        case "applied":
            return "applied"
        case "acknowledged":
            return "acknowledged"
        case "stage_advanced":
            to = ev.to_stage or ev.payload.get("to_stage")
            return to if isinstance(to, str) else None
        case "interview_scheduled":
            stage = ev.payload.get("stage")
            return stage if isinstance(stage, str) else None
        case "offer_received":
            return "offer"
        case "offer_negotiated":
            return "negotiating"
        case "human_corrected":
            if ev.payload.get("field") == "stage":
                to = ev.payload.get("to")
                return to if isinstance(to, str) else None
            return None
        case _:
            return None


def _implied_status(ev: DomainEvent) -> AppStatus | None:
    """The status each event type implies."""
    match ev.type:
        case "rejected":
            return "rejected"
        case "withdrawn":
            return "withdrawn"
        case "accepted":
            return "accepted"
        case "went_silent":
            return "dormant"
        case "human_corrected":
            if ev.payload.get("field") == "status":
                to = ev.payload.get("to")
                return to if isinstance(to, str) else None  # type: ignore[return-value]
            return None
        case _:
            # Any real signal from the world means the process is alive again.
            # This is how a dormant application comes back without anyone
            # touching it — and it cannot override a rejection, because
            # terminal events sit in a higher tier.
            return "live" if ev.type in INBOUND_SIGNALS else None


@dataclass(slots=True)
class FoldProvenance:
    """Which event decided each field — the "why does it say that" question."""

    decided_by: dict[str, DomainEvent] = field(default_factory=dict)
    # Events the floor excluded. They stay in the log; they just do not vote.
    ignored_below_floor: list[DomainEvent] = field(default_factory=list)
    # Set when a terminal status froze automated stage movement.
    frozen_at: DomainEvent | None = None


def _empty_state() -> ApplicationState:
    return ApplicationState(
        current_stage="applied",
        current_phase="sent",
        status="live",
        applied_at=None,
        last_signal_at=None,
        role_title=None,
        seniority=None,
        location=None,
        work_mode=None,
        company_id=None,
        channel=None,
        comp_expectation_minor=None,
        comp_currency=None,
        confidence=0.0,
    )


def fold(events: Iterable[DomainEvent], stages: StageTable | None = None) -> ApplicationState:
    return fold_with_provenance(events, stages)[0]


def fold_with_provenance(
    events: Iterable[DomainEvent],
    stages: StageTable | None = None,
) -> tuple[ApplicationState, FoldProvenance]:
    table = stages or DEFAULT_STAGES
    prov = FoldProvenance()
    all_events = list(events)

    if not all_events:
        return _empty_state(), prov

    # A signal is either good enough to change your pipeline or good enough to
    # be a question, never both. Everything under the floor is a question and
    # lives in the review queue instead.
    voting: list[DomainEvent] = []
    for ev in all_events:
        if ev.confidence < FOLD_CONFIDENCE_FLOOR:
            prov.ignored_below_floor.append(ev)
        else:
            voting.append(ev)

    if not voting:
        return _empty_state(), prov

    def tier_of(ev: DomainEvent, field_name: CorrectableField) -> int:
        if _is_correction_for(ev, field_name):
            return _PINNED_TIER
        if _is_terminal_event(ev):
            return _TERMINAL_TIER
        return _ORDINARY_TIER

    # ── status ──────────────────────────────────────────────────────────────
    status_cands = [
        _Candidate(v, ev, tier_of(ev, "status"))
        for ev in voting
        if (v := _implied_status(ev)) is not None
    ]
    status_win = _pick(status_cands)
    status: AppStatus = status_win.value if status_win else "live"
    if status_win:
        prov.decided_by["status"] = status_win.ev

    # ── the freeze ──────────────────────────────────────────────────────────
    # "Terminal statuses freeze stage changes from automated rungs; a human
    # correction can still reopen" (Spec §10). Reopening works by construction:
    # a correction to a non-terminal status makes the winner non-terminal, and
    # nothing is frozen any more.
    frozen = status_win if status_win and _is_terminal_event(status_win.ev) else None
    if frozen:
        prov.frozen_at = frozen.ev

    def after_freeze(ev: DomainEvent) -> bool:
        if not frozen:
            return False
        if _is_correction_for(ev, "stage"):
            return False
        return _sort_key(_Candidate(0, ev, _ORDINARY_TIER)) > _sort_key(
            _Candidate(0, frozen.ev, _ORDINARY_TIER)
        )

    # ── stage ───────────────────────────────────────────────────────────────
    stage_cands = [
        _Candidate(v, ev, tier_of(ev, "stage"))
        for ev in voting
        if not after_freeze(ev) and (v := _implied_stage(ev)) is not None
    ]
    stage_win = _pick(stage_cands)
    current_stage = stage_win.value if stage_win else "applied"
    if stage_win:
        prov.decided_by["current_stage"] = stage_win.ev
        prov.decided_by["current_phase"] = stage_win.ev

    # ── descriptive fields carried in payloads ──────────────────────────────
    # They live in the event payload rather than only on the row so the row can
    # be dropped and rebuilt from the log (Spec §04, invariant 6).
    def corrected(ev: DomainEvent, name: str) -> Any:
        if ev.type == "human_corrected" and ev.payload.get("field") == name:
            return ev.payload.get("to")
        return None

    def decide(
        read: Callable[[DomainEvent], Any],
        key: str,
        correction_field: CorrectableField,
    ) -> Any:
        cands = [
            _Candidate(v, ev, tier_of(ev, correction_field))
            for ev in voting
            if (v := read(ev)) is not None
        ]
        win = _pick(cands)
        if win:
            prov.decided_by[key] = win.ev
            return win.value
        return None

    role_title: str | None = decide(
        lambda ev: corrected(ev, "role_title") or ev.payload.get("role_title"),
        "role_title",
        "role_title",
    )
    seniority: str | None = decide(
        lambda ev: corrected(ev, "seniority") or ev.payload.get("seniority"),
        "seniority",
        "seniority",
    )
    location: str | None = decide(
        lambda ev: corrected(ev, "location") or ev.payload.get("location"),
        "location",
        "location",
    )
    work_mode: WorkMode | None = decide(
        lambda ev: corrected(ev, "work_mode") or ev.payload.get("work_mode"),
        "work_mode",
        "work_mode",
    )
    company_id: str | None = decide(
        lambda ev: corrected(ev, "company_id") or ev.payload.get("company_id"),
        "company_id",
        "company_id",
    )
    channel: Channel | None = decide(
        lambda ev: corrected(ev, "channel") or ev.payload.get("channel"),
        "channel",
        "channel",
    )

    # Comp expectation is the user's own ask, so only they can set it.
    comp_win = _pick(
        [
            _Candidate(v, ev, tier_of(ev, "comp_expectation"))
            for ev in voting
            if (v := corrected(ev, "comp_expectation")) is not None
        ]
    )

    # ── applied_at ──────────────────────────────────────────────────────────
    # The earliest `applied`, not the latest: after a cross-channel merge the
    # two applications keep the earliest as first touch, and every timing
    # statistic is measured from that instant. A pinned correction overrides.
    applied_correction = _pick(
        [
            _Candidate(datetime.fromisoformat(v), ev, tier_of(ev, "applied_at"))
            for ev in voting
            if isinstance(v := corrected(ev, "applied_at"), str)
        ]
    )
    applied_at: datetime | None = None
    if applied_correction:
        applied_at = applied_correction.value
        prov.decided_by["applied_at"] = applied_correction.ev
    else:
        for ev in voting:
            if ev.type != "applied":
                continue
            if applied_at is None or ev.occurred_at < applied_at:
                applied_at = ev.occurred_at
                prov.decided_by["applied_at"] = ev
        if applied_at is None:
            # An acknowledgement proves an application.
            #
            # §05 gives `applied` as "confirmation mail or quick add", but in
            # real mail the confirmation *is* the acknowledgement — "Thanks for
            # applying to Lexroom" arrives from the ATS and there is no
            # separate event marking the submission, because submitting
            # happened in a web form that never emailed anybody. Leaving
            # `applied_at` null there is not caution, it is a hole: every ratio
            # divides by applications with an applied_at, the 21-day maturity
            # exclusion cannot be evaluated, and time-to-first-response has no
            # zero to measure from.
            for ev in voting:
                if ev.type != "acknowledged":
                    continue
                if applied_at is None or ev.occurred_at < applied_at:
                    applied_at = ev.occurred_at
                    prov.decided_by["applied_at"] = ev

    # ── last_signal_at ──────────────────────────────────────────────────────
    last_signal_at: datetime | None = None
    for ev in voting:
        if ev.type not in INBOUND_SIGNALS:
            continue
        if last_signal_at is None or ev.occurred_at > last_signal_at:
            last_signal_at = ev.occurred_at
            prov.decided_by["last_signal_at"] = ev

    state = ApplicationState(
        current_stage=current_stage,
        current_phase=table.phase_of(current_stage),
        status=status,
        applied_at=applied_at,
        last_signal_at=last_signal_at,
        role_title=role_title,
        seniority=seniority,
        location=location,
        work_mode=work_mode,
        company_id=company_id,
        channel=channel,
        comp_expectation_minor=comp_win.value["minor"] if comp_win else None,
        comp_currency=comp_win.value["currency"].upper() if comp_win else None,
        # The confidence the interface shows next to the stage is the
        # confidence of the claim that produced it, not an average of
        # everything ever seen.
        confidence=stage_win.ev.confidence if stage_win else 0.0,
    )
    if comp_win:
        prov.decided_by["comp_expectation_minor"] = comp_win.ev

    return state, prov
