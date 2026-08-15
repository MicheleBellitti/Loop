"""The corpus, and the harness that reads it.

`fixtures/` is synthetic — written from the same reading of the spec that
produced the rules, which is how CI came to report perfect precision on a
mailbox where almost nothing matched. It is kept as a regression net and read as
one: these tests say the ladder still does what it did, not that it is right.
"""

import pytest

from loop.harness import STALE_FIXTURES, LadderRunner, load_fixtures, parse_eml, summarise

CASES = load_fixtures()
VERDICTS = {
    v.provider_message_id: v for v in LadderRunner().judge_all(c.message for c in CASES)
}

# `STALE_FIXTURES` lives in `loop.harness` because `scripts/corpus_gate.py`
# needs the same list: it takes these two out of the arithmetic, and `strict`
# here makes the xfail fall over on the day one starts passing. Two copies could
# drift into a suite that goes red while the merge gate stays green.


def _case_id(case) -> str:
    return case.path


def _positive(case) -> pytest.param:
    stale = STALE_FIXTURES.get(case.path)
    marks = [pytest.mark.xfail(reason=stale, strict=True)] if stale else []
    return pytest.param(case, marks=marks, id=case.path)


class TestTheFixtureCorpus:
    @pytest.mark.parametrize("case", [_positive(c) for c in CASES if c.expect.get("intent")])
    def test_every_positive_fixture_is_read_as_it_claims(self, case) -> None:
        verdict = VERDICTS[case.path]
        if case.expect.get("requires_model") and verdict.intent is None:
            # Deferring to rung 3 is the documented answer. Reading it without
            # the model is better than the fixture asks for, so it is allowed —
            # the two Italian ones are read by the phrase vocabulary.
            return
        assert verdict.intent == case.expect["intent"]
        if "company" in case.expect:
            assert verdict.company == case.expect["company"]
        if "vendor" in case.expect:
            assert verdict.vendor == case.expect["vendor"]

    @pytest.mark.parametrize("case", [c for c in CASES if c.expect.get("drop")], ids=_case_id)
    def test_every_negative_fixture_is_dropped_or_left_alone(self, case) -> None:
        assert VERDICTS[case.path].intent is None

    def test_no_fixture_the_classifier_must_keep_is_dropped(self) -> None:
        # "Dropping a real application is invisible and unrecoverable."
        missed = [
            c.path
            for c in CASES
            if c.expect.get("intent") and VERDICTS[c.path].outcome == "drop"
        ]
        assert missed == []


class TestTheEmlParser:
    def test_reads_a_multipart_message_and_its_invite(self) -> None:
        raw = (
            "From: Giulia <giulia@nexi.it>\n"
            "Subject: Colloquio\n"
            'Content-Type: multipart/mixed; boundary="b"\n'
            "\n"
            "--b\n"
            "Content-Type: text/plain\n"
            "\n"
            "A domani.\n"
            "--b\n"
            "Content-Type: text/calendar\n"
            "\n"
            "BEGIN:VCALENDAR\n"
            "METHOD:REQUEST\n"
            "BEGIN:VTIMEZONE\n"
            "DTSTART:16011028T030000\n"
            "END:VTIMEZONE\n"
            "BEGIN:VEVENT\n"
            "UID:ev-1\n"
            "SUMMARY:System design interview\n"
            "DTSTART:20260803T140000Z\n"
            "ORGANIZER:mailto:giulia@nexi.it\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
            "--b--\n"
        )
        message = parse_eml(raw, "inline")
        assert message.text.strip() == "A domani."
        assert message.invite is not None
        # Read from inside the VEVENT: a VTIMEZONE carries its own DTSTART in
        # the sixteen-hundreds, and reading the first one put interviews four
        # centuries in the past.
        assert message.invite.starts_at.year == 2026
        assert message.invite.summary == "System design interview"
        assert message.invite.organiser == "giulia@nexi.it"
        assert message.invite.method == "REQUEST"


class TestTheSummary:
    def test_counts_what_was_read_and_by_which_rung(self) -> None:
        counts = summarise(list(VERDICTS.values()))
        assert counts["messages"] == len(CASES)
        assert counts["extracted"] == counts["rung_1"] + counts["rung_2"]
        assert counts["dropped"] + counts["extracted"] + counts["review"] == counts["messages"]
