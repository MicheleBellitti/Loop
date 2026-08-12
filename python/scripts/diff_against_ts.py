"""Diff the Python ladder against the TypeScript, message by message.

    npm run export:baseline                                  # once, in the repo root
    uv run --extra ladder python scripts/diff_against_ts.py

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
from loop.ladder import ClassifierContext


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
    )

    expected: Counter[str] = Counter()
    unexpected: list[str] = []
    agreed = 0

    for case in baseline.cases:
        verdict = runner.judge(case.message)
        fields = differing_fields(case, verdict)
        if not fields:
            agreed += 1
            continue

        deliberate = explain(case, verdict, baseline.context)
        line = _describe(case, verdict, fields)
        if deliberate:
            expected[deliberate.name] += 1
            if args.show_expected:
                print(f"  ~ {deliberate.name}  {line}")
        else:
            unexpected.append(line)

    total = len(baseline.cases)
    print(f"\n  {total} messages · {agreed} identical · {sum(expected.values())} deliberate")
    for name, count in expected.most_common():
        print(f"      {count:>4}  {name}")

    if unexpected:
        print(f"\n  {len(unexpected)} unexplained")
        for line in unexpected:
            print(f"  ✗ {line}")
        return 1

    print("\n  no unexplained differences")
    return 0


def _describe(case: BaselineCase, verdict: Verdict, fields: tuple[str, ...]) -> str:
    changes = "  ".join(
        f"{name}: {getattr(case, name)!r} → {getattr(verdict, name)!r}" for name in fields
    )
    return f"{verdict.provider_message_id}  {changes}"


if __name__ == "__main__":
    raise SystemExit(main())
