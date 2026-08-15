"""The CI grep §12 asks for.

"The service MUST NOT have a send path — there is no code that calls an SMTP or
Gmail send API anywhere in the repo, and a CI grep asserts it."

Loop drafts follow-ups and never has the right to send one; this is the check
that keeps that true as the codebase grows. Web push is not mail and is allowed
— it is the only outbound channel, it needs no mailbox scope, and it cannot
reach a recruiter.

**`.py` is new here.** The Node version's extension list was
`ts|tsx|js|mjs|cjs|jsx|yaml|yml|sql|json`, so from the day the port started, the
invariant CI claimed to enforce was not being checked against the half of the
repository that actually reads the mailbox. It never failed, which is the
problem with a check that cannot fail.

    uv run python scripts/assert_no_send_path.py
"""

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from loop.paths import repo_root

FORBIDDEN: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bnodemailer\b", re.I), "SMTP client"),
    (re.compile(r"\bsmtplib\b"), "Python SMTP client"),
    (re.compile(r"\bSMTP(?:_SSL)?\s*\("), "SMTP connection"),
    (re.compile(r"createTransport\s*\("), "SMTP transport"),
    (re.compile(r"\bsendMail\s*\("), "SMTP send"),
    (re.compile(r"\bsend_message\s*\("), "message send"),
    (re.compile(r"users\.messages\.send"), "Gmail send API"),
    (re.compile(r"/messages/send\b"), "Gmail send API"),
    (re.compile(r"\bgmail\.send\b"), "Gmail send API"),
    (
        re.compile(r"['\"`]https://api\.(sendgrid|mailgun|postmark|resend)", re.I),
        "hosted mail API",
    ),
    (re.compile(r"\bsmtp://", re.I), "SMTP URL"),
    (
        re.compile(r"gmail\.modify|gmail\.compose|mail\.google\.com/[\"'\s]", re.I),
        "a write scope",
    ),
]

# One alternation over all of the above, tried first. The per-pattern loop then
# runs only on the handful of lines it matches, to say *which* rule was broken.
# The scan covers roughly forty thousand lines; thirteen searches each is half a
# million regex passes per CI run to find, in the normal case, nothing.
ANY_FORBIDDEN = re.compile(
    "|".join(f"(?:{pattern.pattern})" for pattern, _ in FORBIDDEN), re.I
)

SKIP_DIRS = frozenset(
    {
        "node_modules",
        "dist",
        ".git",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "design",
        "coverage",
        "fixtures",
    }
)

EXTENSIONS = frozenset(
    {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".yaml", ".yml", ".sql", ".json"}
)

# A line that only talks *about* the rule is not a send path.
TALKING_ABOUT_IT = re.compile(
    r"MUST NOT|never sends?|cannot send|no send path|read-only|drafts? ", re.I
)


@dataclass(frozen=True, slots=True)
class Violation:
    file: Path
    line: int
    why: str
    text: str


def walk(root: Path) -> Iterator[Path]:
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            if entry.name in SKIP_DIRS:
                continue
            yield from walk(entry)
        elif entry.suffix in EXTENSIONS:
            yield entry


def scan(root: Path, *, skip: Path | None = None) -> list[Violation]:
    violations: list[Violation] = []
    for path in walk(root):
        if skip is not None and path.resolve() == skip.resolve():
            continue  # This file names every pattern it forbids.
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not ANY_FORBIDDEN.search(text):
            continue  # The whole file in one pass, which is the usual answer.
        for number, line in enumerate(text.splitlines(), start=1):
            if not ANY_FORBIDDEN.search(line) or TALKING_ABOUT_IT.search(line):
                continue
            for pattern, why in FORBIDDEN:
                if pattern.search(line):
                    violations.append(
                        Violation(path.relative_to(root), number, why, line.strip())
                    )
    return violations


def main() -> None:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else repo_root()
    violations = scan(root, skip=Path(__file__))

    if violations:
        print(
            "\n  A send path reached the repository."
            " Loop drafts follow-ups; it never delivers one.\n",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation.file}:{violation.line}  {violation.why}", file=sys.stderr)
            print(f"      {violation.text[:100]}", file=sys.stderr)
        print("", file=sys.stderr)
        raise SystemExit(1)

    print("no send path — Loop can draft a follow-up and cannot deliver one")


if __name__ == "__main__":
    main()
