"""Turn your own mail into fixtures, locally.

    uv run python scripts/anonymise.py ~/exported-mail

"Build fixtures/ from real mail on day one: anonymise names, addresses and
links with a script, keep the structure byte-for-byte."

Structure is what the rules match on — the sender domain, the subject shape, the
header set — so it survives verbatim. What identifies a person does not:
addresses, display names, links and long digit runs are replaced with stable
pseudonyms, so a thread still reads as one thread.

Output goes to `fixtures/private/`, which is git-ignored. Your inbox is the only
place the go/no-go number can be measured, and it is also the one thing that
must never leave your machine.
"""

import argparse
import hashlib
import re
from pathlib import Path

# Domains that are the whole signal, and therefore must not be rewritten.
KEEP_DOMAINS = (
    "greenhouse-mail.io",
    "greenhouse.io",
    "lever.co",
    "myworkday.com",
    "workday.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "workablemail.com",
    "workable.com",
    "icims.com",
    "taleo.net",
    "recruitee.com",
    "bamboohr.com",
    "linkedin.com",
    "indeed.com",
    "indeedemail.com",
    "allibo.com",
)

_ADDRESS = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_DISPLAY_NAME = re.compile(r"^(From|To|Cc|Reply-To):\s*\"?([^\"<\n]+)\"?\s*<", re.I | re.M)
_URL = re.compile(r"https?://([^\s\"'<>)]+)")
_LONG_DIGITS = re.compile(r"\b\d{6,}\b")


def alias(value: str, prefix: str) -> str:
    """Stable per run of the same input: one address maps to one alias."""
    digest = hashlib.sha256(value.lower().encode()).hexdigest()[:8]
    return f"{prefix}-{digest}"


def keep(domain: str) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in KEEP_DOMAINS)


def anonymise(raw: str) -> str:
    def an_address(match: re.Match[str]) -> str:
        local, domain = match.group(1), match.group(2).lower()
        host = domain if keep(domain) else f"{alias(domain, 'company')}.example"
        return f"{alias(local, 'user')}@{host}"

    def a_display_name(match: re.Match[str]) -> str:
        # Usually a real person, and never load-bearing for a rule — the rules
        # read the employer out of the *ATS* display name, which is a company.
        return f"{match.group(1)}: {alias(match.group(2).strip(), 'name')} <"

    def a_url(match: re.Match[str]) -> str:
        rest = match.group(1)
        host = rest.split("/")[0].lower()
        if keep(host):
            # Keep the vendor, drop the path: a tracking link is a person's
            # identity in a query string.
            return f"https://{host}/{alias(rest, 'path')}"
        return f"https://{alias(host, 'host')}.example/{alias(rest, 'path')}"

    def some_digits(match: re.Match[str]) -> str:
        original = match.group(0)
        digits = re.sub(r"\D", "", alias(original, "id"))
        return digits.ljust(6, "0")[: len(original)]

    out = _ADDRESS.sub(an_address, raw)
    out = _DISPLAY_NAME.sub(a_display_name, out)
    out = _URL.sub(a_url, out)
    return _LONG_DIGITS.sub(some_digits, out)


def backend_root() -> Path:
    """backend/, where `fixtures/` lives."""
    return Path(__file__).resolve().parents[1]


AFTERWARDS = """
  anonymised {written} message(s) into fixtures/private/

  Read a few before you trust it. Then add the expected intent for each one to
  fixtures/manifest.json and run:

      uv run --extra ladder python scripts/corpus_gate.py

  The gate that decides whether Loop is worth building is measured here:
  ≥0.85 application-level recall over twelve months, with zero wrong merges.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a directory of .eml files")
    args = parser.parse_args()

    if not args.source.is_dir():
        raise SystemExit(f"{args.source} is not a directory")

    target = backend_root() / "fixtures" / "private"
    target.mkdir(parents=True, exist_ok=True)

    written = 0
    for path in sorted(args.source.glob("*.eml")):
        # `errors="replace"` rather than a strict decode: an export is whatever
        # the client wrote, and one message with a broken charset must not stop
        # the other nine hundred.
        raw = path.read_text(encoding="utf-8", errors="replace")
        (target / path.name).write_text(anonymise(raw), encoding="utf-8")
        written += 1

    print(AFTERWARDS.format(written=written))


if __name__ == "__main__":
    main()
