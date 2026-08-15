"""Diff the Python ladder against the TypeScript, message by message.

    git checkout 0ceb07c && npm install && npm run export:baseline && git checkout -
    uv run --extra all python scripts/diff_against_ts.py

The exporter is not in this tree: it went with the rest of the TypeScript, and
commit `0ceb07c` is the last one that holds it. `backend/README.md` says the
same thing at more length.

Prints only the disagreements. A difference the divergence table explains is
reported and forgiven; anything else is a porting error and exits non-zero.

This is the phase-1 gate: the port is only trustworthy to the extent that this
runs clean over real mail.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop.harness import (
    BaselineCase,
    LadderRunner,
    Verdict,
    differing_fields,
    explain,
    load_baseline,
)
from loop.ladder import ClassifierContext, RuleRegistry

BASELINE_HOWTO = (
    "git checkout 0ceb07c && npm install && npm run export:baseline && git checkout -"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", help="path to ladder-baseline.jsonl")
    parser.add_argument(
        "--show-expected", action="store_true", help="print the deliberate differences too"
    )
    args = parser.parse_args()

    baseline = load_baseline(Path(args.baseline) if args.baseline else None)
    runner = LadderRunner(
        classifier=ClassifierContext(
            company_domains=baseline.context.company_domains,
            known_threads=baseline.context.known_threads,
            known_newsletters=baseline.context.known_newsletters,
        ),
        thread_to_application=dict(baseline.context.thread_to_application),
        own_addresses=baseline.context.own_addresses,
    )

    # `rules/ats/*.yaml` is shared data: adding a vendor changes both
    # implementations, but only the one that has re-read the files. A difference
    # attributable to a vendor the reference never saw is a stale baseline, not
    # a porting error, and saying so is the difference between a harness that
    # can be trusted and one that cries wolf.
    unseen = _vendors_the_reference_never_saw(baseline.context.ats_vendors)

    expected: Counter[str] = Counter()
    stale: Counter[str] = Counter()
    unexpected: list[str] = []
    agreed = 0

    for case in baseline.cases:
        verdict = runner.judge(case.message)
        fields = differing_fields(case, verdict)
        if not fields:
            agreed += 1
            continue

        line = _describe(case, verdict, fields)
        deliberate = explain(case, verdict, baseline.context)
        if verdict.vendor in unseen:
            stale[verdict.vendor or "?"] += 1
        elif deliberate:
            expected[deliberate.name] += 1
            if args.show_expected:
                print(f"  ~ {deliberate.name}  {line}")
        else:
            unexpected.append(line)

    total = len(baseline.cases)
    print(f"\n  {total} messages · {agreed} identical · {sum(expected.values())} deliberate")
    for name, count in expected.most_common():
        print(f"      {count:>4}  {name}")

    if stale:
        print(f"\n  {sum(stale.values())} from rules the baseline predates")
        for name, count in stale.most_common():
            print(f"      {count:>4}  rules/ats/{name}.yaml")

    if not baseline.context.ats_vendors:
        print("\n  note: this baseline records no registry, so new vendors cannot be told")
        print("        apart from porting errors. Re-exporting from 0ceb07c fixes that:")
        print(f"        {BASELINE_HOWTO}")

    if unexpected:
        print(f"\n  {len(unexpected)} unexplained")
        for line in unexpected:
            print(f"  ✗ {line}")
        return 1

    print("\n  no unexplained differences")
    return 0


def _vendors_the_reference_never_saw(recorded: frozenset[str]) -> frozenset[str]:
    """Vendors in the registry now that the baseline was not judged with.

    An empty record means the baseline predates the field entirely, and nothing
    can be attributed — better to report every difference than to forgive one on
    a guess.
    """
    if not recorded:
        return frozenset()
    return frozenset(rule.vendor for rule in RuleRegistry.load()) - recorded


def _describe(case: BaselineCase, verdict: Verdict, fields: tuple[str, ...]) -> str:
    changes = "  ".join(
        f"{name}: {getattr(case, name)!r} → {getattr(verdict, name)!r}" for name in fields
    )
    return f"{verdict.provider_message_id}  {changes}"


if __name__ == "__main__":
    raise SystemExit(main())
