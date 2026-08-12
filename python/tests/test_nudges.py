from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from loop.domain.nudges import (
    AppSnapshot,
    DeadlineSnapshot,
    InterviewSnapshot,
    NudgeInput,
    evaluate_nudges,
    rank_and_cap,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def app(**over: object) -> AppSnapshot:
    base = {
        "id": "a1",
        "company": "Nexi",
        "role_title": "Platform Engineer",
        "current_stage": "onsite_loop",
        "status": "live",
        "last_signal_at": utc("2026-07-24T09:00:00Z"),
        "awaiting_them": True,
        "last_user_action_at": None,
        "went_dormant_at": None,
    }
    base.update(over)
    return AppSnapshot(**base)  # type: ignore[arg-type]


def inp(**over: object) -> NudgeInput:
    base: dict[str, object] = {
        "now": NOW,
        "applications": [app()],
        "p75_dwell_days": lambda _s: 3.0,
        "p50_dwell_days": lambda _s: 3.0,
    }
    base.update(over)
    return NudgeInput(**base)  # type: ignore[arg-type]


class TestFollowUpDue:
    def test_fires_past_p75_of_the_users_own_history(self) -> None:
        s = evaluate_nudges(inp())
        assert len(s) == 1
        assert s[0].rule == "follow_up_due"
        assert s[0].meta == "6 days quiet"
        assert s[0].cta == "Draft follow-up"

    def test_stays_silent_inside_the_users_normal_wait(self) -> None:
        assert evaluate_nudges(inp(p75_dwell_days=lambda _s: 30.0)) == []

    def test_falls_back_to_stale_after_days_when_there_is_no_history(self) -> None:
        # onsite_loop is stale after 14 days → threshold 8.4, and 6 is inside.
        none_history: dict[str, object] = {
            "p75_dwell_days": lambda _s: None,
            "p50_dwell_days": lambda _s: None,
        }
        assert evaluate_nudges(inp(**none_history)) == []
        older = app(last_signal_at=utc("2026-07-19T09:00:00Z"))  # 11 days
        s = evaluate_nudges(inp(applications=[older], **none_history))
        assert len(s) == 1
        assert "median" not in s[0].body

    def test_does_not_fire_when_the_ball_is_in_the_users_court(self) -> None:
        assert evaluate_nudges(inp(applications=[app(awaiting_them=False)])) == []

    def test_issues_at_most_one_per_application_per_rule(self) -> None:
        assert evaluate_nudges(inp(open_or_issued=frozenset({"follow_up_due:a1"}))) == []


class TestDeadline:
    def test_fires_and_is_the_only_rule_that_bypasses_the_budget(self) -> None:
        s = evaluate_nudges(
            inp(
                deadlines=[
                    DeadlineSnapshot(
                        "a1", "take_home", utc("2026-08-02T21:59:00Z"), "CodeSubmit"
                    )
                ]
            )
        )
        d = next(x for x in s if x.rule == "deadline")
        assert d.bypasses_budget
        assert d.title == "Nexi take-home due Sunday"
        assert d.pushable

    def test_does_not_fire_for_a_deadline_that_already_passed(self) -> None:
        s = evaluate_nudges(
            inp(
                deadlines=[
                    DeadlineSnapshot(
                        "a1", "take_home", utc("2026-07-29T21:59:00Z"), "CodeSubmit"
                    )
                ]
            )
        )
        assert not any(x.rule == "deadline" for x in s)


class TestPrepare:
    def test_fires_inside_forty_eight_hours(self) -> None:
        s = evaluate_nudges(
            inp(
                interviews=[
                    InterviewSnapshot("i1", "a1", "system_design", utc("2026-07-31T08:00:00Z"))
                ]
            )
        )
        p = next(x for x in s if x.rule == "prepare")
        assert p.cta == "Open the brief"
        assert not p.bypasses_budget
        # No advice generation: the body promises only what you already wrote.
        assert "already" in p.body

    def test_does_not_fire_three_days_out(self) -> None:
        s = evaluate_nudges(
            inp(
                interviews=[
                    InterviewSnapshot("i1", "a1", "technical", utc("2026-08-03T08:00:00Z"))
                ]
            )
        )
        assert not any(x.rule == "prepare" for x in s)


class TestLetItGo:
    @staticmethod
    def dormant(id_: str, company: str) -> AppSnapshot:
        return app(
            id=id_,
            company=company,
            status="dormant",
            awaiting_them=False,
            went_dormant_at=utc("2026-07-10T02:00:00Z"),
        )

    def test_batches_into_one_card_and_is_never_pushed(self) -> None:
        s = evaluate_nudges(
            inp(applications=[self.dormant("a1", "Casavo"), self.dormant("a2", "Sportradar")])
        )
        letgo = next(x for x in s if x.rule == "let_it_go")
        assert letgo.title == "Casavo and Sportradar look finished"
        assert letgo.cta == "Archive both"
        assert letgo.application_ids == ("a1", "a2")
        assert not letgo.pushable

    def test_backs_off_once_the_user_has_acted(self) -> None:
        acted = replace(
            self.dormant("a1", "Casavo"), last_user_action_at=utc("2026-07-20T09:00:00Z")
        )
        s = evaluate_nudges(inp(applications=[acted]))
        assert not any(x.rule == "let_it_go" for x in s)

    def test_waits_seven_days_after_dormancy(self) -> None:
        fresh = replace(
            self.dormant("a1", "Casavo"), went_dormant_at=utc("2026-07-28T02:00:00Z")
        )
        s = evaluate_nudges(inp(applications=[fresh]))
        assert not any(x.rule == "let_it_go" for x in s)


class TestTheDisplayBudget:
    def test_shows_at_most_three_ranked_by_urgency_then_depth(self) -> None:
        apps = [
            app(id="a1", company="Nexi", current_stage="onsite_loop"),
            app(id="a2", company="Docebo", current_stage="hr_call"),
            app(id="a3", company="Everli", current_stage="applied"),
        ]
        s = evaluate_nudges(
            inp(
                applications=apps,
                p75_dwell_days=lambda _s: 1.0,
                deadlines=[
                    DeadlineSnapshot(
                        "a2", "take_home", utc("2026-08-01T12:00:00Z"), "CodeSubmit"
                    )
                ],
                interviews=[
                    InterviewSnapshot("i1", "a3", "technical", utc("2026-07-31T08:00:00Z"))
                ],
            )
        )
        ranked = rank_and_cap(s)
        assert len(ranked) == 3
        assert [x.rule for x in ranked] == ["deadline", "prepare", "follow_up_due"]
        # Within follow_up_due the deeper stage comes first.
        assert ranked[2].application_ids[0] == "a1"
