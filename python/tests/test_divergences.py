"""The harness has to be trustworthy before anything it reports is.

Its whole job is to tell a deliberate improvement from a porting mistake, so a
predicate that forgives too much is worse than no harness at all: it would
report a clean diff over a broken port.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from loop.domain.messages import CalendarInvite, MessageHeaders, RawMessage
from loop.harness import BaselineCase, Verdict, differing_fields, explain, load_baseline
from loop.harness.corpus import BaselineContext

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def message(
    *,
    sender: str = "Giulia <giulia@nexi.it>",
    thread_id: str | None = None,
    invite: CalendarInvite | None = None,
) -> RawMessage:
    return RawMessage(
        user_id="u",
        mailbox_id="m",
        provider_message_id="id",
        thread_id=thread_id,
        received_at=NOW,
        headers=MessageHeaders(message_id="<1@x>", sender=sender, subject="", date=""),
        text="",
        body_sha256="",
        invite=invite,
    )


def before(**over: object) -> BaselineCase:
    base: dict[str, object] = {
        "message": message(),
        "score": 5,
        "outcome": "pass",
        "intent": None,
        "company": None,
        "role": None,
        "confidence": None,
        "rung": None,
        "vendor": None,
        "stage_hint": None,
    }
    base.update(over)
    return BaselineCase(**base)  # type: ignore[arg-type]


def after(**over: object) -> Verdict:
    base: dict[str, object] = {"provider_message_id": "id", "score": 5, "outcome": "pass"}
    base.update(over)
    return Verdict(**base)  # type: ignore[arg-type]


EMPTY = BaselineContext()


class TestDifferingFields:
    def test_says_nothing_when_the_two_agree(self) -> None:
        assert differing_fields(before(intent="rejected"), after(intent="rejected")) == ()

    def test_names_every_field_that_moved(self) -> None:
        fields = differing_fields(
            before(intent="rejected", company="Nexi"), after(intent="offer", company="Prima")
        )
        assert fields == ("intent", "company")


class TestStageAbstention:
    def test_forgives_an_untitled_invite_that_no_longer_guesses(self) -> None:
        found = explain(
            before(intent="interview_invite", stage_hint="technical"),
            after(intent="interview_invite", stage_hint=None),
            EMPTY,
        )
        assert found is not None
        assert found.name == "stage-abstention"

    def test_does_not_forgive_a_stage_that_simply_changed(self) -> None:
        assert (
            explain(
                before(intent="interview_invite", stage_hint="technical"),
                after(intent="interview_invite", stage_hint="final"),
                EMPTY,
            )
            is None
        )


class TestThreadVocabulary:
    def test_forgives_a_rejection_read_on_a_thread_the_system_owns(self) -> None:
        found = explain(
            before(message=message(thread_id="t1")),
            after(intent="rejected", rung=2, confidence=0.88),
            BaselineContext(thread_to_application={"t1": "app-1"}),
        )
        assert found is not None
        assert found.name == "thread-vocabulary"

    def test_does_not_forgive_rung_two_reading_a_thread_nobody_owns(self) -> None:
        # Otherwise every over-eager phrase match would be waved through as an
        # improvement.
        assert explain(before(), after(intent="rejected", rung=2), EMPTY) is None


class TestPracticeSites:
    def test_forgives_dropping_a_leetcode_promotion(self) -> None:
        found = explain(
            before(
                message=message(sender="LeetCode <no-reply@leetcode.com>"), intent="take_home"
            ),
            after(),
            EMPTY,
        )
        assert found is not None
        assert found.name == "practice-sites"

    def test_does_not_forgive_losing_a_real_take_home(self) -> None:
        assert explain(before(intent="take_home"), after(), EMPTY) is None


class TestCompanyFromDomain:
    def test_forgives_the_name_the_senders_own_domain_yields(self) -> None:
        found = explain(
            before(intent="rejected"), after(intent="rejected", company="Nexi"), EMPTY
        )
        assert found is not None
        assert found.name == "company-from-domain"

    def test_does_not_forgive_a_company_that_came_from_nowhere(self) -> None:
        assert (
            explain(before(intent="rejected"), after(intent="rejected", company="Prima"), EMPTY)
            is None
        )


class TestLoadingABaseline:
    def test_reads_the_context_line_and_the_messages_after_it(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.jsonl"
        path.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "kind": "context",
                            "company_domains": ["nexi.it"],
                            "known_threads": ["t1"],
                            "thread_to_application": {"t1": "app-1"},
                        }
                    ),
                    json.dumps(
                        {
                            "message": {
                                "provider_message_id": "m1",
                                "thread_id": "t1",
                                "received_at": "2026-07-30T09:00:00Z",
                                "headers": {
                                    "from": "Giulia <giulia@nexi.it>",
                                    "subject": "Ciao",
                                },
                                "text": "Purtroppo non proseguiremo.",
                            },
                            "verdict": {"score": 5, "outcome": "pass", "intent": "rejected"},
                        }
                    ),
                )
            ),
            encoding="utf-8",
        )

        baseline = load_baseline(path)
        assert baseline.context.company_domains == frozenset({"nexi.it"})
        assert baseline.context.thread_to_application == {"t1": "app-1"}
        assert len(baseline.cases) == 1
        case = baseline.cases[0]
        assert case.message.headers.sender == "Giulia <giulia@nexi.it>"
        assert case.message.received_at == datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
        assert case.intent == "rejected"
