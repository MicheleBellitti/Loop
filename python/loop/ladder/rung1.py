"""Rung 1 — the ATS template registry.

The first pattern that fits wins, and an unmatched vendor abstains rather than
producing a low-confidence intent: falling through to rung 2 costs 2 ms, while a
guess costs a wrong application that the user has to find and correct.
"""

from dataclasses import dataclass

from loop.domain import domain_of_address
from loop.domain.messages import CandidateMessage, MessageHeaders

from .company import company_from_display_name, company_from_domain
from .contracts import Extraction, LadderContext
from .phrases import match_phrase
from .regex import first_group
from .registry import Pattern, Rule
from .role import role_from_body


@dataclass(frozen=True, slots=True)
class TemplateRung:
    costly: bool = False

    def extract(self, msg: CandidateMessage, ctx: LadderContext) -> Extraction | None:
        sender_domain = domain_of_address(msg.headers.sender)
        if not sender_domain:
            return None

        for rule in ctx.registry:
            if not rule.sends_from(sender_domain) or not _headers_match(rule, msg):
                continue
            return _apply(rule, msg, sender_domain, ctx)
        return None


def _apply(
    rule: Rule, msg: CandidateMessage, sender_domain: str, ctx: LadderContext
) -> Extraction | None:
    haystack = f"{msg.headers.subject}\n{msg.text}"

    for pattern in rule.patterns:
        fields = _match(pattern, msg, haystack)
        if fields is None:
            continue
        return Extraction(
            intent=pattern.intent,
            confidence=pattern.confidence,
            rung=1,
            company=_company(rule, msg, sender_domain, fields, ctx),
            # The subject rarely names the job; the confirmation body usually does.
            role=fields.get("role") or role_from_body(haystack),
            deadline=fields.get("deadline"),
        )

    # The vendor is known but none of its own templates fit. Before giving up,
    # try the cross-vendor vocabulary: the sender is already established as an
    # ATS writing to this user about an application, so the only question left
    # is which kind of message this is. Without it, a vendor's rule file has to
    # enumerate every phrasing every one of its customers uses in every
    # language — Ashby delivers rejections written in Italian by an Italian
    # company, and an English-only rule file simply never sees them.
    phrase = match_phrase(haystack)
    if phrase is None:
        return None
    return Extraction(
        intent=phrase.intent,
        confidence=phrase.confidence,
        rung=1,
        company=company_from_display_name(msg.headers.sender),
        role=role_from_body(haystack),
    )


def _match(pattern: Pattern, msg: CandidateMessage, haystack: str) -> dict[str, str] | None:
    """The fields a pattern captures, or None when it does not fit."""
    fields: dict[str, str] = {}

    if pattern.subject is not None:
        found = pattern.subject.search(msg.headers.subject)
        if found is None:
            return None
        fields.update(
            {name: value.strip() for name, value in found.groupdict().items() if value}
        )

    if pattern.body is not None and not pattern.body.search(msg.text):
        return None

    for name, extractor in pattern.extract.items():
        captured = first_group(extractor, haystack, name)
        if captured:
            fields[name] = captured

    return fields


def _company(
    rule: Rule,
    msg: CandidateMessage,
    sender_domain: str,
    fields: dict[str, str],
    ctx: LadderContext,
) -> str | None:
    if rule.company_from == "sender_display_name":
        return company_from_display_name(msg.headers.sender) or fields.get("company")

    captured = fields.get("company")
    if captured:
        return captured
    if rule.company_from == "sender_domain":
        return company_from_domain(sender_domain, ctx.registry.ats_domains)
    return company_from_display_name(msg.headers.sender)


def _headers_match(rule: Rule, msg: CandidateMessage) -> bool:
    """A null in the YAML means "the header must simply be present"."""
    for name, wanted in rule.headers.items():
        actual = _header(msg.headers, name)
        if actual is None or (wanted is not None and wanted not in actual):
            return False
    return True


# The one header whose name is a Python keyword.
_HEADER_ALIASES = {"from": "sender"}


def _header(headers: MessageHeaders, name: str) -> str | None:
    key = name.lower().replace("-", "_")
    value = getattr(headers, _HEADER_ALIASES.get(key, key), None)
    return value if isinstance(value, str) else None
