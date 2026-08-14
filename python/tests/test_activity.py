"""The activity ladder — what counts as a pipeline and what is just history."""

from datetime import UTC, datetime, timedelta

import pytest

from loop.domain.activity import activity_of, closure_days, is_open

NOW = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def judge(**overrides: object) -> str:
    base: dict[str, object] = {
        "now": NOW,
        "status": "live",
        "current_stage": "hr_call",
        "current_phase": "screening",
        "presumed_closed": False,
        "last_signal_at": days_ago(2),
        "next_interview_at": None,
        "quiet_threshold_days": 21,
    }
    return activity_of(**{**base, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["rejected", "withdrawn", "accepted", "dormant"])
def test_a_recorded_outcome_outranks_everything(status: str) -> None:
    assert judge(status=status, last_signal_at=days_ago(1)) == "closed"


def test_an_interview_in_the_diary_beats_any_silence() -> None:
    booked = NOW + timedelta(days=3)
    assert judge(last_signal_at=days_ago(200), next_interview_at=booked) == "active"


def test_the_sweep_is_trusted_once_it_has_presumed_closure() -> None:
    assert judge(presumed_closed=True) == "closed"


@pytest.mark.parametrize("stage", ["take_home", "offer", "negotiating"])
def test_a_stage_waiting_on_you_is_never_written_off(stage: str) -> None:
    assert judge(current_stage=stage, last_signal_at=days_ago(300)) == "active"


def test_an_unanswered_application_closes_after_two_months() -> None:
    acknowledged = {"current_stage": "acknowledged", "current_phase": "sent"}
    assert judge(**acknowledged, last_signal_at=days_ago(59)) == "stale"
    assert judge(**acknowledged, last_signal_at=days_ago(61)) == "closed"


def test_a_process_that_got_somewhere_gets_the_full_ninety_days() -> None:
    assert judge(last_signal_at=days_ago(61)) == "stale"
    assert judge(last_signal_at=days_ago(91)) == "closed"


def test_stale_sits_between_the_stage_threshold_and_closure() -> None:
    assert judge(last_signal_at=days_ago(20)) == "active"
    assert judge(last_signal_at=days_ago(22)) == "stale"


def test_the_stage_default_stands_in_without_a_cadence_of_your_own() -> None:
    assert judge(quiet_threshold_days=None, last_signal_at=days_ago(22)) == "stale"


def test_a_row_with_no_signal_yet_is_active() -> None:
    assert judge(last_signal_at=None) == "active"


def test_silence_before_a_reply_is_judged_sooner_than_after_one() -> None:
    assert closure_days("sent") == 60
    assert closure_days("screening") == 90
    assert closure_days("interviewing") == 90


def test_quiet_is_still_open_because_a_follow_up_is_worth_sending() -> None:
    assert is_open("active")
    assert is_open("stale")
    assert not is_open("closed")
