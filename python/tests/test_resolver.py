import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loop.domain.messages import CalendarInvite, Comp, Signal
from loop.resolver import (
    Ambiguous,
    Attached,
    Candidate,
    Created,
    LexicalEmbedder,
    cosine,
    country_of,
    decide,
    domain_label,
    events_for_signal,
    find_duplicate,
    merge_is_forbidden,
    parse_vector,
    plan_lookup,
    to_vector,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
EMBED = LexicalEmbedder()


def signal(**over: object) -> Signal:
    base: dict[str, object] = {
        "user_id": "u",
        "mailbox_id": "m",
        "provider_message_id": "id",
        "evidence_ref": "id",
        "intent": "acknowledged",
        "occurred_at": NOW,
        "confidence": 0.95,
        "rung": 1,
        "language": "en",
    }
    base.update(over)
    return Signal(**base)  # type: ignore[arg-type]


def candidate(id_: str, role: str, **over: object) -> Candidate:
    base: dict[str, object] = {"id": id_, "embedding": EMBED.embed(role)}
    base.update(over)
    return Candidate(**base)  # type: ignore[arg-type]


class TestTheEmbedder:
    """It is ported bit-for-bit, and that is the point.

    The resolver's thresholds were tuned against this function's output. A
    different geometry with the same numbers would be a silent regression, so
    the reference vectors are frozen here and the port is held to them.
    """

    REFERENCE = json.loads(
        (Path(__file__).parent / "data" / "embeddings-reference.json").read_text(
            encoding="utf-8"
        )
    )

    @pytest.mark.parametrize("title", list(REFERENCE))
    def test_matches_the_typescript_to_the_last_bit(self, title: str) -> None:
        expected = self.REFERENCE[title]
        got = EMBED.embed(title)
        assert len(got) == len(expected)
        assert max((abs(a - b) for a, b in zip(got, expected, strict=True)), default=0) < 1e-15

    def test_a_title_is_nearer_to_its_own_rephrasing_than_to_another_job(self) -> None:
        backend = EMBED.embed("backend engineer")
        assert cosine(backend, EMBED.embed("back end engineer")) > cosine(
            backend, EMBED.embed("product designer")
        )

    def test_an_empty_title_is_a_zero_vector_and_matches_nothing(self) -> None:
        assert cosine(EMBED.embed(""), EMBED.embed("backend engineer")) == 0

    def test_round_trips_through_the_postgres_literal(self) -> None:
        vector = EMBED.embed("machine learning engineer")
        assert parse_vector(to_vector(vector)) == vector
        assert parse_vector(None) == []


class TestChoosingAnApplication:
    def test_creates_one_when_the_company_has_none(self) -> None:
        assert isinstance(
            decide(signal(role="Backend Engineer"), EMBED.embed("x"), []), Created
        )

    def test_attaches_to_the_one_open_application_when_the_role_agrees(self) -> None:
        found = decide(
            signal(role="Backend Engineer", role_normalised="backend engineer"),
            EMBED.embed("backend engineer"),
            [candidate("a1", "backend engineer")],
        )
        assert isinstance(found, Attached)
        assert found.application_id == "a1"

    def test_but_creates_a_second_when_it_is_plainly_a_different_job(self) -> None:
        found = decide(
            signal(role="Product Designer", role_normalised="product designer"),
            EMBED.embed("product designer"),
            [candidate("a1", "backend engineer")],
        )
        assert isinstance(found, Created)

    def test_a_roleless_signal_joins_the_only_application_at_the_company(self) -> None:
        # Most real mail does not repeat the job title. Treating a rejection
        # that says only "we will not be moving forward" as a new application
        # turned one employer into four rows and lost the real one's rejection.
        found = decide(signal(), EMBED.embed("unknown"), [candidate("a1", "backend engineer")])
        assert isinstance(found, Attached)
        assert found.cosine == 1.0

    def test_but_asks_when_the_company_has_several(self) -> None:
        found = decide(
            signal(),
            EMBED.embed("unknown"),
            [candidate("a1", "backend engineer"), candidate("a2", "data scientist")],
        )
        assert isinstance(found, Ambiguous)
        assert {c for c, _ in found.candidates} == {"a1", "a2"}

    def test_asks_rather_than_guesses_between_two_close_candidates(self) -> None:
        found = decide(
            signal(role="Backend Engineer", role_normalised="backend engineer"),
            EMBED.embed("backend engineer"),
            [candidate("a1", "backend engineer"), candidate("a2", "backend engineer")],
        )
        assert isinstance(found, Ambiguous)

    def test_and_shows_at_most_three(self) -> None:
        found = decide(
            signal(),
            EMBED.embed("unknown"),
            [candidate(f"a{i}", f"role {i}") for i in range(5)],
        )
        assert isinstance(found, Ambiguous)
        assert len(found.candidates) == 3


class TestMerging:
    """A wrong merge rewrites history silently, so the exclusions are named."""

    @pytest.mark.parametrize(
        ("over", "reason"),
        [
            ({"current_stage": "offer"}, "an offer or negotiation is open"),
            ({"current_stage": "negotiating"}, "an offer or negotiation is open"),
            ({"status": "accepted"}, "one of them was accepted"),
            ({"manually_created": True}, "one of them was declared by hand"),
        ],
    )
    def test_refuses_and_says_why(self, over: dict[str, object], reason: str) -> None:
        a = candidate("a1", "backend engineer", **over)
        b = candidate("a2", "backend engineer")
        assert merge_is_forbidden(a, b) == reason
        # And in either order — the exclusion is about the pair, not the argument.
        assert merge_is_forbidden(b, a) == reason

    def test_refuses_across_a_border(self) -> None:
        a = candidate("a1", "backend engineer", location="Milan")
        b = candidate("a2", "backend engineer", location="Berlin")
        assert merge_is_forbidden(a, b) == "different country"

    def test_but_not_between_two_cities_in_one_country(self) -> None:
        a = candidate("a1", "backend engineer", location="Milano")
        b = candidate("a2", "backend engineer", location="Bologna")
        assert merge_is_forbidden(a, b) is None

    def test_never_undoes_a_split_a_human_made(self) -> None:
        a = candidate("a1", "backend engineer", split_from=frozenset({"a2"}))
        b = candidate("a2", "backend engineer")
        assert merge_is_forbidden(a, b) == "a human split them before"

    def test_merges_the_same_job_found_twice_and_keeps_the_earlier(self) -> None:
        earlier = candidate("a1", "backend engineer", applied_at=NOW - timedelta(days=2))
        later = candidate("a2", "backend engineer", applied_at=NOW)
        merge = find_duplicate(later, [earlier])
        assert merge is not None
        assert (merge.keep, merge.merge) == ("a1", "a2")

    def test_leaves_two_applications_months_apart_alone(self) -> None:
        old = candidate("a1", "backend engineer", applied_at=NOW - timedelta(days=90))
        new = candidate("a2", "backend engineer", applied_at=NOW)
        assert find_duplicate(new, [old]) is None

    def test_and_two_different_jobs_at_one_company(self) -> None:
        assert (
            find_duplicate(candidate("a1", "backend engineer"), [candidate("a2", "recruiter")])
            is None
        )


class TestCompanyIdentity:
    def test_prefers_the_domain_because_it_cannot_be_spelled_twice(self) -> None:
        plan = plan_lookup(signal(company="ION Group", sender_domain="iongroup.com"), ())
        assert plan.domain == "iongroup.com"
        assert plan.alias == "iongroup"

    def test_never_treats_the_ats_domain_as_the_employer(self) -> None:
        plan = plan_lookup(
            signal(company="Prima", sender_domain="hire.eu.lever.co"), ("lever.co",)
        )
        assert plan.domain is None
        assert plan.name == "Prima"

    def test_records_both_spellings_so_the_next_one_finds_this_row(self) -> None:
        # "ION Group" from a display name and "iongroup" from the domain were
        # two companies, two pipelines and two sets of statistics.
        plan = plan_lookup(signal(company="ION Group", sender_domain="iongroup.com"), ())
        assert plan.aliases_to_record == ("iongroup",)

    def test_falls_back_to_a_readable_name_from_the_domain(self) -> None:
        plan = plan_lookup(signal(sender_domain="bendingspoons.com"), ())
        assert plan.name == "bendingspoons"

    @pytest.mark.parametrize(
        ("domain", "label"),
        [("iongroup.com", "iongroup"), ("talent.nexi.it", "nexi"), ("acme.co.uk", "acme")],
    )
    def test_reads_the_registrable_label(self, domain: str, label: str) -> None:
        assert domain_label(domain) == label

    def test_and_admits_when_it_cannot(self) -> None:
        assert domain_label(None) is None
        assert country_of("Atlantis") == "unknown"


class TestSignalsBecomingEvents:
    def test_an_acknowledgement_advances_to_acknowledged(self) -> None:
        events = events_for_signal(signal(intent="acknowledged"), "a1")
        assert [e.type for e in events] == ["acknowledged"]
        assert events[0].to_stage == "acknowledged"

    def test_an_untitled_invitation_advances_the_phase_and_not_the_round(self) -> None:
        # The third place the `technical` default lived. An invitation whose
        # title names no round proves the process reached interviewing and
        # nothing at all about which interview it is.
        events = events_for_signal(
            signal(
                intent="interview_invite",
                invite=CalendarInvite(uid="ev1", summary="Chat", starts_at=NOW),
            ),
            "a1",
        )
        assert [e.type for e in events] == ["interview_scheduled"]
        assert events[0].payload["stage"] == "interview"
        assert events[0].to_stage == "interview"

    def test_a_titled_one_keeps_the_round_it_names(self) -> None:
        events = events_for_signal(
            signal(intent="interview_invite", stage_hint="system_design"), "a1"
        )
        assert events[0].payload["stage"] == "system_design"

    def test_a_cancellation_withdraws_the_claim_rather_than_advancing_it(self) -> None:
        # The TypeScript said so in a comment and then passed a stage anyway,
        # which the fold reads — so a cancelled round moved the application
        # forward.
        events = events_for_signal(
            signal(
                intent="interview_cancelled",
                stage_hint="technical",
                invite=CalendarInvite(uid="ev1", summary="x", starts_at=NOW, method="CANCEL"),
            ),
            "a1",
        )
        assert len(events) == 1
        assert "stage" not in events[0].payload
        assert events[0].to_stage is None
        assert events[0].payload["status"] == "cancelled"

    def test_a_cancellation_with_no_invite_says_nothing_at_all(self) -> None:
        assert events_for_signal(signal(intent="interview_cancelled"), "a1") == []

    def test_a_take_home_with_a_deadline_records_both(self) -> None:
        events = events_for_signal(
            signal(intent="take_home", deadline="2026-08-02T21:59:00Z"), "a1"
        )
        assert [e.type for e in events] == ["stage_advanced", "deadline_set"]
        assert events[1].payload["kind"] == "take_home"

    def test_an_offer_carries_its_money(self) -> None:
        events = events_for_signal(
            signal(intent="offer", comp=Comp(currency="EUR", min_minor=5_500_000)), "a1"
        )
        assert events[0].type == "offer_received"
        assert events[0].payload["min_minor"] == 5_500_000
        assert events[0].to_stage == "offer"

    def test_only_an_application_claims_the_first_touch(self) -> None:
        applied = events_for_signal(signal(intent="applied", channel="linkedin"), "a1")
        acked = events_for_signal(signal(intent="acknowledged", channel="linkedin"), "a1")
        assert applied[0].source is not None
        assert applied[0].source.is_first_touch
        assert acked[0].source is not None
        assert not acked[0].source.is_first_touch

    def test_a_signal_with_no_channel_carries_no_provenance(self) -> None:
        assert events_for_signal(signal(intent="applied"), "a1")[0].source is None

    @pytest.mark.parametrize("intent", ["other", "unclear"])
    def test_an_abstention_produces_nothing(self, intent: str) -> None:
        # Inventing a stage change out of silence is the failure this system
        # exists to avoid.
        assert events_for_signal(signal(intent=intent), "a1") == []
