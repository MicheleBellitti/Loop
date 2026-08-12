"""What the Python ladder reads, over a corpus.

    uv run --extra ladder python scripts/replay.py                 # fixtures
    uv run --extra ladder python scripts/replay.py --baseline      # real mail
    uv run --extra ladder python scripts/replay.py --baseline --show-review

Prints what was extracted and by which rung, and — with `--show-review` — the
subjects of everything that fell through, which is the list the next rule is
written from. Read-only, and local: subjects go to the terminal and nowhere
else.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop.harness import LadderRunner, load_baseline, load_fixtures, summarise
from loop.ladder import ClassifierContext


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        nargs="?",
        const="",
        help="replay the private baseline instead of the committed fixtures",
    )
    parser.add_argument("--show-review", action="store_true", help="list what fell through")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many messages")
    args = parser.parse_args()

    if args.baseline is not None:
        baseline = load_baseline(Path(args.baseline) if args.baseline else None)
        messages = [case.message for case in baseline.cases]
        runner = LadderRunner(
            classifier=ClassifierContext(
                company_domains=baseline.context.company_domains,
                known_threads=baseline.context.known_threads,
                known_newsletters=baseline.context.known_newsletters,
            ),
            thread_to_application=dict(baseline.context.thread_to_application),
            own_addresses=baseline.context.own_addresses,
        )
        source = "baseline"
    else:
        messages = [case.message for case in load_fixtures()]
        runner = LadderRunner()
        source = "fixtures"

    if args.limit:
        messages = messages[: args.limit]

    verdicts = runner.judge_all(messages)
    counts = summarise(verdicts)

    print(f"\n  {source}\n  {'─' * len(source)}")
    for name, value in counts.items():
        print(f"  {name.replace('_', ' '):<14} {value}")

    extracted = [v for v in verdicts if v.intent]
    if extracted:
        print("\n  read")
        width = max(len(v.intent or "") for v in extracted)
        for verdict in sorted(extracted, key=lambda v: (v.intent or "", v.company or "")):
            company = verdict.company or "—"
            role = f"  · {verdict.role}" if verdict.role else ""
            print(f"  rung {verdict.rung}  {verdict.intent:<{width}}  {company}{role}")

    if args.show_review:
        by_id = {m.provider_message_id: m for m in messages}
        pending = [v for v in verdicts if v.outcome != "drop" and not v.intent]
        print(f"\n  fell through ({len(pending)})")
        for verdict in pending:
            headers = by_id[verdict.provider_message_id].headers
            print(f"  [{verdict.score:+d}] {headers.sender}\n        {headers.subject}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
