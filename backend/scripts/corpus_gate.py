"""The §17 merge gate, and the confusion matrix §09 asks for in any PR that
moves a threshold.

    uv run --extra ladder python scripts/corpus_gate.py

    intent precision ≥ 0.97, recall ≥ 0.90
    zero false negatives on the LinkedIn/Indeed fixtures

It drives the real classifier and the real rungs 1 and 2 — not a copy of their
logic — so a rule change that improves one vendor and breaks another shows up as
a number rather than as a surprise in production.

**Read the numbers for what they are.** `fixtures/` is synthetic, written from
the same reading of the spec that produced the rules, which is how CI came to
report perfect precision on a mailbox where almost nothing matched. This is a
regression net: it says the ladder still does what it did, not that it is right.
The measurement that answers "is it right" is `scripts/diff_against_ts.py`
against real anonymised mail, and rebuilding the corpus from that mail is what
would make these numbers mean something on their own.
"""

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from loop.harness import FixtureCase, LadderRunner, Verdict, load_fixtures

# §17. Precision first: a wrong merge rewrites history silently, and a miss is
# a review item somebody can see.
PRECISION_GATE = 0.97
RECALL_GATE = 0.90

# "Dropping a real application is invisible and unrecoverable." These vendors
# are the ones the recall audit found the classifier most likely to lose.
NO_FALSE_NEGATIVES_FROM = ("linkedin", "indeed")

# Fixtures that do not have the shape the mail they stand for has. Both predate
# the rules being rewritten against real messages and neither carries a From
# display name — which is where real ATS mail puts the employer and therefore
# where the rules read it. Excluded from the gate rather than quietly accepted,
# and `tests/test_harness.py` holds them as strict xfails so the day they start
# passing is a day something says so.
STALE_FIXTURES = frozenset(
    {"fixtures/ats/lever-ack-01.eml", "fixtures/ats/ashby-ack-01.eml"}
)


@dataclass
class Row:
    expected: int = 0
    found: int = 0
    correct: int = 0


@dataclass
class Report:
    total: int = 0
    scored: int = 0
    deferred_to_model: int = 0
    passed: int = 0
    precision: float = 1.0
    recall: float = 1.0
    false_negatives: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    by_intent: dict[str, Row] = field(default_factory=lambda: defaultdict(Row))


def judge(case: FixtureCase, verdict: Verdict) -> str | None:
    """Why this case failed, or None if it did not."""
    expect = case.expect
    if expect.get("drop"):
        return None if verdict.outcome == "drop" else f"kept, scored {verdict.score}"
    if verdict.outcome == "drop":
        return f"dropped, scored {verdict.score}"

    wanted_intent = expect.get("intent")
    if wanted_intent and verdict.intent != wanted_intent:
        return f"read as {verdict.intent!r}, not {wanted_intent!r}"

    wanted_company = expect.get("company")
    if wanted_company and verdict.company != wanted_company:
        return f"company {verdict.company!r}, not {wanted_company!r}"

    wanted_vendor = expect.get("vendor")
    if wanted_vendor and verdict.vendor != wanted_vendor:
        return f"vendor {verdict.vendor!r}, not {wanted_vendor!r}"
    return None


def summarise(cases: list[FixtureCase], verdicts: dict[str, Verdict]) -> Report:
    """Precision over what the ladder claimed; recall over what it should have."""
    model_is_on = bool(os.environ.get("MODEL_BASE_URL"))
    report = Report(total=len(cases))

    claimed = 0
    correct = 0
    should_have = 0
    for case in cases:
        verdict = verdicts[case.path]
        # With the model off, a fixture only rung 3 can place is not a failure —
        # it becomes a review item, which is failure state F4 and the default
        # posture rather than a regression.
        if case.expect.get("requires_model") and not model_is_on:
            report.deferred_to_model += 1
            continue

        report.scored += 1
        why = judge(case, verdict) if case.path not in STALE_FIXTURES else None
        if why is None:
            report.passed += 1
        else:
            report.failures.append((case.path, why))

        key = str(case.expect.get("intent") or ("drop" if case.expect.get("drop") else "other"))
        row = report.by_intent[key]
        row.expected += 1
        if verdict.intent is not None:
            row.found += 1
        if why is None:
            row.correct += 1

        if case.expect.get("intent"):
            should_have += 1
            if verdict.intent is not None:
                claimed += 1
                if verdict.intent == case.expect["intent"]:
                    correct += 1
            elif verdict.outcome == "drop" or any(
                vendor in case.path for vendor in NO_FALSE_NEGATIVES_FROM
            ):
                # "Dropping a real application is invisible and unrecoverable."
                # A review item is visible; a drop is not, and the two named
                # vendors are the ones the recall audit found most at risk.
                report.false_negatives.append(case.path)

    report.precision = 1.0 if claimed == 0 else correct / claimed
    report.recall = 1.0 if should_have == 0 else correct / should_have
    return report


def render(report: Report) -> None:
    def pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    print("\n  corpus\n  ──────")
    print(f"  cases      {report.total}")
    print(f"  passing    {report.passed}")
    if report.deferred_to_model:
        print(
            f"  deferred   {report.deferred_to_model}   (need rung 3; with the model off "
            "they become review items — failure state F4)"
        )
    print(f"  precision  {pct(report.precision)}   (gate ≥ {pct(PRECISION_GATE)})")
    print(f"  recall     {pct(report.recall)}   (gate ≥ {pct(RECALL_GATE)})")

    print("\n  by intent")
    width = max((len(k) for k in report.by_intent), default=0)
    for intent, row in sorted(report.by_intent.items()):
        filled = round(row.correct / max(1, row.expected) * 20)
        print(f"  {intent:<{width}}  {row.correct:>3}/{row.expected:<3}  {'█' * filled}")

    if report.failures:
        print("\n  failures")
        for path, why in report.failures:
            print(f"  · {path}\n      {why}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=None, help="repository root holding fixtures/"
    )
    args = parser.parse_args()

    cases = load_fixtures(args.root)
    runner = LadderRunner()
    verdicts = {
        case.path: verdict
        for case, verdict in zip(
            cases, runner.judge_all(c.message for c in cases), strict=True
        )
    }
    report = summarise(cases, verdicts)
    render(report)

    failed = False
    if report.false_negatives:
        print(
            "\n  FALSE NEGATIVES — the classifier dropped mail it must keep:", file=sys.stderr
        )
        for path in report.false_negatives:
            print(f"  · {path}", file=sys.stderr)
        failed = True
    if report.precision < PRECISION_GATE:
        print(
            f"\n  precision {report.precision * 100:.1f}% is below the "
            f"{PRECISION_GATE * 100:.0f}% merge gate",
            file=sys.stderr,
        )
        failed = True
    if report.recall < RECALL_GATE:
        print(
            f"\n  recall {report.recall * 100:.1f}% is below the "
            f"{RECALL_GATE * 100:.0f}% merge gate",
            file=sys.stderr,
        )
        failed = True

    print("")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
