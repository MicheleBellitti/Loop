"""Which keys identify an employer, and in what order to try them.

Domain first, because it is the only key that cannot be spelled two ways. The
alias second, keyed on letters and digits alone, so "ION Group" arriving from an
ATS display name and "iongroup" derived from the company's own domain land on
one row instead of forking the pipeline in two.

The lookups themselves belong to the shell. What lives here is the policy — the
order, and what may be used as a key at all — because that is the part a wrong
answer duplicates an employer over, and the part worth testing without a
database.
"""

from dataclasses import dataclass

from loop.domain import company_key
from loop.domain.messages import Signal
from loop.ladder.domains import in_list


@dataclass(frozen=True, slots=True)
class CompanyLookup:
    """The keys to try, in order, and the name to create under if none hit."""

    # The employer's own domain, or None when the sender is a vendor.
    domain: str | None
    # `company_key` of whatever the ladder read, or None when it read nothing.
    alias: str | None
    # What a newly created company should be called.
    name: str

    @property
    def aliases_to_record(self) -> tuple[str, ...]:
        """Every spelling that should point at the row once it exists."""
        keys = {self.alias, company_key(self.name)} - {None, ""}
        return tuple(sorted(k for k in keys if k))


def plan_lookup(signal: Signal, ats_domains: tuple[str, ...]) -> CompanyLookup:
    # An ATS is never the employer, so its domain must not become one.
    domain = None if in_list(signal.sender_domain, ats_domains) else signal.sender_domain
    company = (signal.company or "").strip()
    return CompanyLookup(
        domain=domain,
        alias=company_key(company) if company else None,
        # A company created from a bare domain still deserves a readable name,
        # and the aliases above are keyed the same way, so the next spelling of
        # it finds this row rather than making another.
        name=company or domain_label(domain) or "Unknown",
    )


def domain_label(domain: str | None) -> str | None:
    """`iongroup.com` → `iongroup`. The registrable label, not the whole host."""
    if not domain:
        return None
    parts = [p for p in domain.split(".") if p]
    if len(parts) < 2:
        return domain
    # Two-part public suffixes: .co.uk, .com.br.
    compound = len(parts) >= 3 and len(parts[-1]) <= 3 and len(parts[-2]) <= 3
    return parts[-3] if compound else parts[-2]
