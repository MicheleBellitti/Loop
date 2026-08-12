"""Where an employer's name comes from.

ATS mail is addressed from the company but delivered by the vendor, so the
display name carries the employer with a recruiting suffix bolted on: "Lexroom
Hiring Team", "Air Apps Recruiting", "Prima via Lever". Those suffixes are the
only thing standing between the header and a clean company, so they come off —
and anything that is plainly not a company returns None, so the caller falls
back rather than inventing an employer.
"""

import re

from loop.domain import domain_of_address

from .domains import in_list, names_an_employer

_DISPLAY_NAME = re.compile(r"""^\s*"?([^"<]+?)"?\s*<""")
_VIA_VENDOR = re.compile(r"\s+via\s+\w+\s*$", re.IGNORECASE)
_RECRUITING_SUFFIX = re.compile(
    r"\s*(?:\b(?:hiring|recruiting|recruitment|talent(?:\s+acquisition)?|careers?|jobs?"
    r"|people(?:\s+ops)?|hr)\b\s*)*(?:\bteam\b)?\s*$",
    re.IGNORECASE,
)
# "Careers @ Jet" and "Recruiting | Acme": the employer is what follows.
_RECRUITING_PREFIX = re.compile(
    r"^(?:careers?|jobs?|recruit(?:ing|ment)|talent|hr|people)\s*[@|·:–—-]\s*", re.IGNORECASE
)
_TRAILING_PUNCTUATION = re.compile(r"[|·–—-]\s*$")

# A run-together robot name — "noreplyHRrecruitingTeam" — has no spaces to put a
# word boundary against, so the suffix stripping cannot see it. It is not a
# company, and guessing one from it is worse than admitting none.
_ROBOT_FRAGMENT = re.compile(r"noreply|donotreply|recruiting|hrteam|talentteam", re.IGNORECASE)
_ROBOT_NAME = re.compile(
    r"^(?:no[-\s._]?reply|do[-\s._]?not[-\s._]?reply|notifications?|support|info|admin)$",
    re.IGNORECASE,
)


def company_from_display_name(from_header: str) -> str | None:
    match = _DISPLAY_NAME.match(from_header)
    if match is None:
        return None
    name = _RECRUITING_SUFFIX.sub("", _VIA_VENDOR.sub("", match.group(1).strip())).strip()
    name = _RECRUITING_PREFIX.sub("", name).strip()
    name = _TRAILING_PUNCTUATION.sub("", name).strip()
    if not name:
        return None
    if " " not in name and _ROBOT_FRAGMENT.search(name):
        return None
    if _ROBOT_NAME.match(name):
        return None
    return name


def company_from_domain(
    address_or_domain: str | None, ats_domains: tuple[str, ...]
) -> str | None:
    """ "talent.nexi.it" → "Nexi".

    The resolver canonicalises this against the company table anyway, but the
    fallback is sometimes the row that gets created first — and a company called
    "nexi" in the interface looks like a bug even when the matching is right.

    Accepts a bare domain as well as an address. The TypeScript took only
    addresses and was called with a bare domain in one place, where it therefore
    always returned null and the fallback never fired.
    """
    if not address_or_domain:
        return None
    domain = (
        domain_of_address(address_or_domain)
        if "@" in address_or_domain
        else address_or_domain.lower()
    )
    # An ATS is never the employer, and neither is a personal mailbox.
    if domain is None or not names_an_employer(domain) or in_list(domain, ats_domains):
        return None
    labels = domain.split(".")
    if len(labels) < 2:
        return None
    return " ".join(word.capitalize() for word in labels[-2].split("-") if word) or None
