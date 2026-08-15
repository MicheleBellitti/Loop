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
from functools import lru_cache
from pathlib import Path

# Domains that are the whole signal, and therefore must not be rewritten.
#
# Read out of `rules/ats/*.yaml` rather than copied, because a copy is a copy
# that drifts: the hand-written list here was missing `taleo.com`,
# `myworkdayjobs.com` and `oraclecloud.com`, so this script was rewriting the
# `sender_domains` rung 1 matches on and the corpus it produced reported recall
# misses that were its own doing. `linkedin.com` and the rest are not vendors
# with a rule file, so they stay written out.
_NOT_IN_THE_RULES = (
    "linkedin.com",
    "indeed.com",
    "indeedemail.com",
    "allibo.com",
)


def keep_domains() -> tuple[str, ...]:
    from loop.ladder import RuleRegistry

    return tuple(dict.fromkeys(RuleRegistry.load().ats_domains + _NOT_IN_THE_RULES))


_ADDRESS = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_DISPLAY_NAME = re.compile(r"^(From|To|Cc|Reply-To):\s*\"?([^\"<\n]+)\"?\s*<", re.I | re.M)
_URL = re.compile(r"https?://([^\s\"'<>)]+)")
_LONG_DIGITS = re.compile(r"\b\d{6,}\b")


def alias(value: str, prefix: str) -> str:
    """Stable per run of the same input: one address maps to one alias."""
    digest = hashlib.sha256(value.lower().encode()).hexdigest()[:8]
    return f"{prefix}-{digest}"


@lru_cache(maxsize=1)
def _keepers() -> tuple[str, ...]:
    return keep_domains()


def keep(domain: str) -> bool:
    """`loop.domain.matches_domain_suffix`, which is the one rule for this."""
    from loop.domain import matches_domain_suffix

    return any(matches_domain_suffix(domain, d) for d in _keepers())


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
        """A pseudonym of exactly the same length, from the whole digest.

        Keeping only the decimal characters of an eight-character hex digest
        left on average five, padded to six — so a fourteen-digit Workday
        requisition became six characters and roughly half of all inputs
        collapsed into a space far smaller than the one they came from. Rules
        that match on the length of a digit run were then measuring a corpus
        shaped differently from the mail it stands for.
        """
        original = match.group(0)
        digest = hashlib.sha256(f"id:{original}".encode()).digest()
        stream = int.from_bytes(digest, "big")
        out = []
        for _ in range(len(original)):
            stream, digit = divmod(stream, 10)
            out.append(str(digit))
        return "".join(out)

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
