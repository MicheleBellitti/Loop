from datetime import UTC, datetime

from loop.domain.flags import Flag, compute_flag, days_quiet, quiet_label
from loop.domain.headline import build_headline, date_eyebrow, number_word
from loop.domain.metrics import (
    channel_gate,
    dwell_metric,
    format_percent,
    ratio,
    seasonal_gate,
)
from loop.domain.stages import display_stage
from loop.domain.types import DomainEvent, EventType

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
TZ = "Europe/Rome"


def utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def ev(type_: EventType, iso: str, **extra: object) -> DomainEvent:
    return DomainEvent(type=type_, occurred_at=utc(iso), confidence=0.95, **extra)  # type: ignore[arg-type]


# ── flags ───────────────────────────────────────────────────────────────────


class TestDaysQuiet:
    def test_floors_to_whole_days_and_never_goes_negative(self) -> None:
        assert days_quiet(NOW, utc("2026-07-24T10:00:00Z")) == 5
        assert days_quiet(NOW, utc("2026-07-31T10:00:00Z")) == 0
        assert days_quiet(NOW, None) is None

    def test_reads_as_english_in_the_row_meta(self) -> None:
        assert quiet_label(1) == "quiet 1 day"
        assert quiet_label(6) == "quiet 6 days"
        assert quiet_label(None) == ""


class TestFlagPrecedence:
    def test_a_deadline_outranks_everything_else(self) -> None:
        f = compute_flag(
            now=NOW,
            tz=TZ,
            status="live",
            deadline_at=utc("2026-08-02T21:59:00Z"),
            decide_by=utc("2026-08-08T00:00:00Z"),
            last_signal_at=utc("2026-06-01T00:00:00Z"),
            quiet_threshold_days=10,
        )
        assert f.kind == "deadline"
        assert f.text.startswith("Due Sunday ")

    def test_ignores_a_deadline_further_out_than_the_window(self) -> None:
        f = compute_flag(
            now=NOW,
            tz=TZ,
            status="live",
            deadline_at=utc("2026-08-20T21:59:00Z"),
            decide_by=utc("2026-08-08T00:00:00Z"),
        )
        assert f == Flag("decide", "decide by 8 Aug")

    def test_falls_through_to_quiet_when_nothing_is_owed(self) -> None:
        f = compute_flag(
            now=NOW,
            tz=TZ,
            status="live",
            last_signal_at=utc("2026-07-14T09:00:00Z"),
            quiet_threshold_days=10,
        )
        assert f == Flag("quiet", "quiet · past your p90")

    def test_a_closed_application_carries_no_flag(self) -> None:
        for status in ("rejected", "withdrawn", "accepted"):
            f = compute_flag(
                now=NOW,
                tz=TZ,
                status=status,  # type: ignore[arg-type]
                deadline_at=utc("2026-07-31T09:00:00Z"),
                last_signal_at=utc("2026-01-01T09:00:00Z"),
                quiet_threshold_days=1,
            )
            assert f == Flag("none", "")

    def test_is_silent_when_everything_is_on_time(self) -> None:
        f = compute_flag(
            now=NOW,
            tz=TZ,
            status="live",
            last_signal_at=utc("2026-07-29T09:00:00Z"),
            quiet_threshold_days=10,
        )
        assert f.kind == "none"


# ── headline ────────────────────────────────────────────────────────────────


def _headline(events: list[DomainEvent], live: int = 14, suggestions: int = 3):  # type: ignore[no-untyped-def]
    return build_headline(
        events=events,
        application_id_of=lambda e: str(e.evidence_ref or "a"),
        live_count=live,
        open_suggestion_count=suggestions,
        now=NOW,
    )


class TestTheTodayHeadline:
    def test_counts_applications_not_events(self) -> None:
        events = [
            ev(
                "stage_advanced",
                "2026-07-28T09:00:00Z",
                evidence_ref="a",
                from_stage="hr_call",
                to_stage="technical",
            ),
            ev(
                "stage_advanced",
                "2026-07-29T09:00:00Z",
                evidence_ref="a",
                from_stage="technical",
                to_stage="final",
            ),
            ev("offer_received", "2026-07-27T09:00:00Z", evidence_ref="b"),
            ev("interview_scheduled", "2026-07-29T09:00:00Z", evidence_ref="c"),
        ]
        h = _headline(events)
        assert h.moved_count == 3
        assert h.lines == ("Three moved", "forward", "this week")

    def test_a_backwards_stage_change_is_not_progress(self) -> None:
        events = [
            ev(
                "stage_advanced",
                "2026-07-29T09:00:00Z",
                evidence_ref="a",
                from_stage="final",
                to_stage="technical",
            )
        ]
        assert _headline(events).kind != "moved"

    def test_ignores_events_outside_the_week(self) -> None:
        events = [ev("offer_received", "2026-07-01T09:00:00Z", evidence_ref="a")]
        assert _headline(events).moved_count == 0

    def test_falls_back_to_a_statement_of_fact_never_to_cheer(self) -> None:
        h = _headline([], live=9, suggestions=2)
        assert h.lines == ("Nine applications", "waiting")

    def test_says_the_day_is_clear_when_nothing_needs_the_user(self) -> None:
        assert _headline([], live=12, suggestions=0).lines == ("You are", "clear today")

    def test_says_nothing_is_tracked_yet_on_day_one(self) -> None:
        assert _headline([], live=0, suggestions=0).lines == ("Nothing", "to track yet")

    def test_never_exceeds_three_lines(self) -> None:
        for live in (0, 1, 9, 40):
            for suggestions in (0, 3):
                assert len(_headline([], live=live, suggestions=suggestions).lines) <= 3


def test_number_word_spells_small_numbers_then_falls_back() -> None:
    assert number_word(3) == "Three"
    assert number_word(12) == "Twelve"
    assert number_word(13) == "13"


def test_date_eyebrow_renders_the_design_copy_exactly() -> None:
    assert date_eyebrow(utc("2026-07-30T09:00:00Z"), TZ) == "Thursday 30 July"


# ── metrics ─────────────────────────────────────────────────────────────────


class TestRatio:
    def test_always_carries_a_note_naming_its_denominator(self) -> None:
        m = ratio(
            numerator=11,
            denominator=68,
            excluded=5,
            closed=30,
            exclusion_reason="too recent to count",
        )
        assert m.gate_met
        assert m.note == "11 of 68 · 5 too recent to count"
        assert format_percent(m.value) == "16.2%"

    def test_withholds_the_number_below_the_gate_and_names_the_threshold(self) -> None:
        m = ratio(numerator=2, denominator=9, closed=4)
        assert m.value is None
        assert not m.gate_met
        assert m.note == "4 closed · unlocks at 8 closed applications"

    def test_flags_a_small_sample_between_the_two_gates(self) -> None:
        m = ratio(numerator=3, denominator=11, closed=11)
        assert m.small_sample
        assert "small sample" in m.note

    def test_does_not_divide_by_zero(self) -> None:
        assert ratio(numerator=0, denominator=0, closed=10).value is None


class TestOtherGates:
    def test_holds_time_in_stage_until_five_transitions_exist(self) -> None:
        assert dwell_metric(4.2, 4).value is None
        assert dwell_metric(4.2, 4).note == "4 of 5 stage changes needed"
        assert dwell_metric(4.2, 5).value == 4.2

    def test_holds_a_channel_row_under_three_applications(self) -> None:
        assert not channel_gate(2)[0]
        assert channel_gate(3)[0]

    def test_explains_why_seasonal_shape_is_hidden(self) -> None:
        assert "2 more quarters" in seasonal_gate(0)[1]
        assert "1 more quarter" in seasonal_gate(1)[1]
        assert seasonal_gate(2)[0]


def test_format_percent_renders_one_decimal_and_drops_a_trailing_zero() -> None:
    assert format_percent(0.27) == "27%"
    assert format_percent(0.162) == "16.2%"
    assert format_percent(None) == "—"


# ── display stage ───────────────────────────────────────────────────────────


class TestDisplayStage:
    def test_status_wins_over_the_stage_label(self) -> None:
        assert display_stage("rejected", "technical") == "Rejected"
        assert display_stage("accepted", "offer") == "Accepted"

    def test_dormant_says_closed_by_silence_past_the_long_threshold(self) -> None:
        assert display_stage("dormant", "technical") == "Dormant"
        assert (
            display_stage("dormant", "technical", presumed_closed=True) == "Closed by silence"
        )

    def test_a_live_application_shows_its_stage_label(self) -> None:
        assert display_stage("live", "onsite_loop") == "Onsite loop"
