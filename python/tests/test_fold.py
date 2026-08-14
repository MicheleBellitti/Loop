"""The fold's invariants.

These assertions are the specification. They were ported from
`packages/domain/src/fold.test.ts` before the implementation, and each one
exists because the original Engineering Spec said something that does not work
— the reason is on the test, not only in the commit message.
"""

from dataclasses import replace
from datetime import UTC, datetime, timezone
from typing import Any

import pytest

from loop.domain.fold import fold, fold_with_provenance
from loop.domain.types import DomainEvent, EventType

_seq = 0


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def ev(
    type_: EventType,
    occurred: str,
    confidence: float,
    **extra: Any,
) -> DomainEvent:
    global _seq
    _seq += 1
    extra.setdefault("evidence_ref", f"msg-{_seq}")
    extra.setdefault("rung", 1)
    return DomainEvent(
        type=type_,
        occurred_at=at(occurred),
        confidence=confidence,
        id=_seq,
        **extra,
    )


def shuffled(xs: list[DomainEvent], seed: int) -> list[DomainEvent]:
    """Deterministic shuffle so a failure is reproducible."""
    out = list(xs)
    s = seed
    for i in range(len(out) - 1, 0, -1):
        s = (s * 1103515245 + 12345) % 2147483648
        j = s % (i + 1)
        out[i], out[j] = out[j], out[i]
    return out


class TestTheCaseTheSpecGotWrong:
    def test_advances_past_a_high_confidence_auto_reply(self) -> None:
        # The exact sequence the Architecture sheet walks: an ATS
        # acknowledgement at 0.99 followed eleven days later by a recruiter
        # reply at 0.94. Under the literal §05 rule ("highest confidence wins")
        # the stage would stay at `acknowledged` forever.
        events = [
            ev("applied", "2026-07-02T09:00:00Z", 1.0, rung=4, evidence_ref=None),
            ev("acknowledged", "2026-07-02T09:04:00Z", 0.99),
            ev("stage_advanced", "2026-07-13T11:00:00Z", 0.94, to_stage="hr_call"),
        ]
        s = fold(events)
        assert s.current_stage == "hr_call"
        assert s.current_phase == "screening"
        assert s.confidence == 0.94

    def test_a_correction_beats_a_later_higher_confidence_event(self) -> None:
        events = [
            ev("applied", "2026-07-02T09:00:00Z", 1.0),
            ev("stage_advanced", "2026-07-10T09:00:00Z", 0.95, to_stage="technical"),
            ev(
                "human_corrected",
                "2026-07-11T09:00:00Z",
                1.0,
                rung=4,
                evidence_ref=None,
                payload={"field": "stage", "from": "technical", "to": "hr_call"},
            ),
            ev("stage_advanced", "2026-07-12T09:00:00Z", 0.99, to_stage="technical"),
        ]
        # The pin is older than the last automated event but still wins: "the
        # agent is never allowed to argue with you twice".
        assert fold(events).current_stage == "hr_call"

    def test_quick_add_does_not_pin(self) -> None:
        # Quick add writes at confidence 1.0 because you typed it. If that
        # pinned the stage, a hand-added application would be deaf to its own
        # mailbox forever.
        events = [
            ev("applied", "2026-07-02T09:00:00Z", 1.0, rung=4, evidence_ref=None),
            ev("stage_advanced", "2026-07-10T09:00:00Z", 0.9, to_stage="technical"),
        ]
        assert fold(events).current_stage == "technical"


class TestDeterminism:
    events = [
        ev("applied", "2026-06-12T08:00:00Z", 1.0),
        ev("acknowledged", "2026-06-13T08:00:00Z", 0.99),
        ev("stage_advanced", "2026-07-01T08:00:00Z", 0.95, to_stage="hr_call"),
        ev("interview_scheduled", "2026-07-15T08:00:00Z", 0.97, payload={"stage": "technical"}),
        ev("stage_advanced", "2026-07-24T08:00:00Z", 0.90, to_stage="onsite_loop"),
        ev("went_silent", "2026-08-20T02:00:00Z", 0.90, rung=None, evidence_ref=None),
    ]

    def test_any_arrival_order_yields_the_same_state(self) -> None:
        reference = fold(self.events)
        for seed in range(1, 201):
            assert fold(shuffled(self.events, seed)) == reference

    def test_does_not_depend_on_the_serial_id(self) -> None:
        # The spec's tie-break on `id` would have made this fail, because `id`
        # is a serial and therefore arrival order.
        a = [replace(e, id=i) for i, e in enumerate(self.events)]
        b = [replace(e, id=len(self.events) - i) for i, e in enumerate(self.events)]
        assert fold(a) == fold(b)


class TestStatus:
    def test_a_new_signal_revives_a_dormant_application(self) -> None:
        events = [
            ev("applied", "2026-05-01T08:00:00Z", 1.0),
            ev("went_silent", "2026-06-20T02:00:00Z", 0.9, rung=None, evidence_ref=None),
            ev("stage_advanced", "2026-06-25T08:00:00Z", 0.93, to_stage="hr_call"),
        ]
        assert fold(events).status == "live"

    def test_went_silent_never_touches_the_stage(self) -> None:
        events = [
            ev("applied", "2026-05-01T08:00:00Z", 1.0),
            ev("stage_advanced", "2026-05-10T08:00:00Z", 0.93, to_stage="technical"),
            ev("went_silent", "2026-06-20T02:00:00Z", 0.9, rung=None, evidence_ref=None),
        ]
        s = fold(events)
        assert s.status == "dormant"
        assert s.current_stage == "technical"
        # The funnel keeps it in its denominator precisely because the stage
        # stands.
        assert s.current_phase == "interviewing"

    def test_a_rejection_is_not_undone_by_a_later_automated_signal(self) -> None:
        events = [
            ev("applied", "2026-05-01T08:00:00Z", 1.0),
            ev("rejected", "2026-06-01T08:00:00Z", 0.97, payload={"after_stage": "technical"}),
            ev("stage_advanced", "2026-06-05T08:00:00Z", 0.99, to_stage="final"),
        ]
        s = fold(events)
        assert s.status == "rejected"
        # Frozen: "how far did it get" must not be rewritten by stray later mail.
        assert s.current_stage != "final"

    def test_a_correction_reopens_a_rejection_and_unfreezes_the_stage(self) -> None:
        events = [
            ev("applied", "2026-05-01T08:00:00Z", 1.0),
            ev("rejected", "2026-06-01T08:00:00Z", 0.97),
            ev(
                "human_corrected",
                "2026-06-02T08:00:00Z",
                1.0,
                rung=4,
                evidence_ref=None,
                payload={"field": "status", "from": "rejected", "to": "live"},
            ),
            ev("stage_advanced", "2026-06-05T08:00:00Z", 0.95, to_stage="final"),
        ]
        s = fold(events)
        assert s.status == "live"
        assert s.current_stage == "final"


class TestTheConfidenceFloor:
    def test_ignores_events_below_the_review_threshold_but_keeps_them_visible(self) -> None:
        weak = ev("stage_advanced", "2026-07-20T08:00:00Z", 0.54, to_stage="offer", rung=3)
        events = [ev("applied", "2026-07-01T08:00:00Z", 1.0), weak]
        state, prov = fold_with_provenance(events)
        assert state.current_stage == "applied"
        assert len(prov.ignored_below_floor) == 1
        assert prov.ignored_below_floor[0].confidence == 0.54


class TestDatesAndPayloadFields:
    def test_applied_at_is_the_earliest_applied_event(self) -> None:
        events = [
            ev("applied", "2026-07-20T08:00:00Z", 0.98, payload={"channel": "linkedin"}),
            ev("applied", "2026-07-12T08:00:00Z", 0.98, payload={"channel": "career_page"}),
        ]
        assert fold(events).applied_at == at("2026-07-12T08:00:00Z")

    def test_applied_at_falls_back_to_the_acknowledgement(self) -> None:
        # The shape of every real ATS thread: you submitted through a web form,
        # so the only trace is "Thanks for applying".
        events = [
            ev("acknowledged", "2026-03-04T10:00:00Z", 0.98, payload={"ats_vendor": "lever"}),
            ev("stage_advanced", "2026-03-20T10:00:00Z", 0.95, to_stage="hr_call"),
        ]
        assert fold(events).applied_at == at("2026-03-04T10:00:00Z")

    def test_a_real_applied_event_still_wins_over_the_fallback(self) -> None:
        events = [
            ev("applied", "2026-03-01T10:00:00Z", 1.0),
            ev("acknowledged", "2026-03-04T10:00:00Z", 0.98),
        ]
        assert fold(events).applied_at == at("2026-03-01T10:00:00Z")

    def test_last_signal_at_ignores_notes_and_corrections(self) -> None:
        events = [
            ev("applied", "2026-07-01T08:00:00Z", 1.0),
            ev("acknowledged", "2026-07-01T08:05:00Z", 0.99),
            ev(
                "note_added",
                "2026-07-30T08:00:00Z",
                1.0,
                rung=None,
                evidence_ref=None,
                payload={"text": "ask about the team"},
            ),
        ]
        assert fold(events).last_signal_at == at("2026-07-01T08:05:00Z")

    def test_carries_descriptive_fields_so_the_row_can_be_rebuilt(self) -> None:
        events = [
            ev(
                "applied",
                "2026-07-01T08:00:00Z",
                0.98,
                payload={
                    "role_title": "Backend Engineer",
                    "seniority": "senior",
                    "location": "Milan",
                    "work_mode": "hybrid",
                    "company_id": "c-1",
                    "channel": "career_page",
                },
            )
        ]
        s = fold(events)
        assert s.role_title == "Backend Engineer"
        assert s.work_mode == "hybrid"
        assert s.company_id == "c-1"
        assert s.channel == "career_page"

    def test_a_correction_pins_a_descriptive_field(self) -> None:
        events = [
            ev("applied", "2026-07-01T08:00:00Z", 0.98, payload={"role_title": "Backend Eng"}),
            ev(
                "human_corrected",
                "2026-07-02T08:00:00Z",
                1.0,
                rung=4,
                evidence_ref=None,
                payload={
                    "field": "role_title",
                    "from": "Backend Eng",
                    "to": "Platform Engineer",
                },
            ),
            ev(
                "stage_advanced",
                "2026-07-09T08:00:00Z",
                0.99,
                to_stage="hr_call",
                payload={"role_title": "Backend Eng"},
            ),
        ]
        assert fold(events).role_title == "Platform Engineer"


class TestEdges:
    def test_an_empty_log_folds_to_a_usable_neutral_state(self) -> None:
        s = fold([])
        assert s.status == "live"
        assert s.applied_at is None
        assert s.confidence == 0.0

    def test_an_offer_sets_stage_and_phase_together(self) -> None:
        events = [
            ev("applied", "2026-06-02T08:00:00Z", 1.0),
            ev(
                "offer_received",
                "2026-07-28T08:00:00Z",
                0.90,
                payload={"min_minor": 6_800_000, "currency": "EUR", "decide_by": "2026-08-08"},
            ),
        ]
        s = fold(events)
        assert s.current_stage == "offer"
        assert s.current_phase == "decided"
        assert s.status == "live"


@pytest.mark.parametrize("tz", [UTC])
def test_timestamps_are_timezone_aware(tz: timezone) -> None:
    # "All timestamps are timestamptz in UTC" — a naive datetime compared
    # against an aware one raises, so this is a real failure mode.
    s = fold([ev("applied", "2026-07-01T08:00:00Z", 1.0)])
    assert s.applied_at is not None and s.applied_at.tzinfo is not None


class TestAPayloadNobodyCanRetract:
    """The log is append-only, so a value that raises raises for ever.

    A correction's `to` is whatever the route wrote — the API does not check its
    shape — and it lands in an event that cannot be taken back. A fold that
    throws on one takes that application's projection down on every rebuild
    from then on, so every read here decides nothing rather than raising.
    """

    def test_a_comp_correction_that_is_not_an_amount_decides_nothing(self) -> None:
        state = fold(
            [
                ev("applied", "2026-07-01T09:00:00Z", 1.0),
                ev(
                    "human_corrected",
                    "2026-07-02T09:00:00Z",
                    1.0,
                    payload={"field": "comp_expectation", "to": 65000},
                ),
            ]
        )
        assert state.comp_expectation_minor is None
        assert state.comp_currency is None

    def test_a_comp_correction_missing_its_currency_decides_nothing(self) -> None:
        state = fold(
            [
                ev("applied", "2026-07-01T09:00:00Z", 1.0),
                ev(
                    "human_corrected",
                    "2026-07-02T09:00:00Z",
                    1.0,
                    payload={"field": "comp_expectation", "to": {"minor": 6500000}},
                ),
            ]
        )
        assert state.comp_expectation_minor == 6500000
        assert state.comp_currency is None

    def test_an_applied_at_that_is_not_a_date_is_ignored(self) -> None:
        state = fold(
            [
                ev("applied", "2026-07-01T09:00:00Z", 1.0),
                ev(
                    "human_corrected",
                    "2026-07-02T09:00:00Z",
                    1.0,
                    payload={"field": "applied_at", "to": "01/07/2026"},
                ),
            ]
        )
        assert state.applied_at == at("2026-07-01T09:00:00Z")

    def test_a_date_with_no_time_stays_comparable_to_every_other_instant(self) -> None:
        # Naive here would poison the first `now - applied_at` it met.
        state = fold(
            [
                ev("applied", "2026-07-01T09:00:00Z", 1.0),
                ev(
                    "human_corrected",
                    "2026-07-02T09:00:00Z",
                    1.0,
                    payload={"field": "applied_at", "to": "2026-06-15"},
                ),
            ]
        )
        assert state.applied_at is not None
        assert state.applied_at.tzinfo is not None
        assert (at("2026-07-01T09:00:00Z") - state.applied_at).days == 16


class TestACancelledInvitation:
    """A round that was called off must stop asserting the stage it booked.

    The resolver no longer puts `stage` in a cancellation's payload, so nothing
    it writes today depends on this. The log is append-only and the TypeScript
    wrote plenty that do — folding one of those without the guard advances an
    application to a round that never happened.
    """

    def test_it_does_not_advance_the_stage_it_had_booked(self) -> None:
        state = fold(
            [
                ev("applied", "2026-07-01T09:00:00Z", 1.0),
                ev(
                    "interview_scheduled",
                    "2026-07-02T09:00:00Z",
                    0.95,
                    payload={"status": "cancelled", "stage": "technical"},
                ),
            ]
        )
        assert state.current_stage == "applied"

    def test_a_confirmed_one_still_does(self) -> None:
        state = fold(
            [
                ev("applied", "2026-07-01T09:00:00Z", 1.0),
                ev(
                    "interview_scheduled",
                    "2026-07-02T09:00:00Z",
                    0.97,
                    payload={"status": "confirmed", "stage": "technical"},
                ),
            ]
        )
        assert state.current_stage == "technical"

    def test_a_cancellation_does_not_undo_a_round_that_already_happened(self) -> None:
        # Dropping the claim is not the same as reversing it: the stage the
        # earlier events reached stands until a human says otherwise.
        state = fold(
            [
                ev("applied", "2026-07-01T09:00:00Z", 1.0),
                ev("stage_advanced", "2026-07-02T09:00:00Z", 0.9, to_stage="hr_call"),
                ev(
                    "interview_scheduled",
                    "2026-07-03T09:00:00Z",
                    0.95,
                    payload={"status": "cancelled", "stage": "technical"},
                ),
            ]
        )
        assert state.current_stage == "hr_call"
