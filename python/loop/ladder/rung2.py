"""Rung 2 — calendar and phrase heuristics.

"An .ics attachment or a meeting link from a company domain resolves the stage
by itself." 13% of messages, 2 ms, €0.

Two changes from the TypeScript, both recorded in `docs/port-to-python.md`:

  · Thread identity is no longer computed here. A reply on a known thread
    inherits its application with no parsing at all, which is an assertion about
    *identity* and not about intent — the ladder reads it once for every rung
    instead. In the TypeScript that assertion was dressed as an extraction with
    `intent: other`, and returning it early meant a rejection arriving on a
    known thread was never read by the vocabulary below.

  · An invitation whose title names no stage abstains instead of defaulting to
    `technical` (§3.2). A default wearing a reading's clothes is what produced a
    pipeline column of identical "Technical" stages.
"""

import re
from dataclasses import dataclass

from loop.domain import domain_of_address
from loop.domain.messages import CandidateMessage

from .company import company_from_display_name, company_from_domain
from .contracts import Extraction, LadderContext
from .domains import LEARNING_PLATFORMS, in_list
from .phrases import match_phrase

# The stage a calendar title implies.
#
# Interview invitations name their own stage more often than any other signal in
# the mailbox — "System & code review", "HR screening", "Final round" — and a
# title match is both cheaper and more reliable than asking a model.
_TITLE_TO_STAGE: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), stage)
    for p, stage in (
        (r"\b(?:final|exec|leadership|founder|ceo|cto)\b", "final"),
        (r"\b(?:onsite|on-site|loop|super\s*day|assessment centre|full day)\b", "onsite_loop"),
        (r"\b(?:system\s*design|architecture|design (?:round|interview))\b", "system_design"),
        (
            r"\b(?:technical|coding|live\s*cod|pair\s*program|algorithm|code review)\b",
            "technical",
        ),
        (r"\b(?:take[- ]home|assignment|exercise|challenge)\b", "take_home"),
        (
            r"\b(?:hr|people|talent|recruiter|screening|intro|introductory|first call"
            r"|knowledge)\b",
            "hr_call",
        ),
    )
)

# "Every .ics invite from a company domain is a near-certain interview."
_INVITE_CONFIDENCE = 0.97
_CANCELLED_CONFIDENCE = 0.95
# One step below the same phrase from a known ATS: the sender is not
# established, so the claim is weaker even when the words are identical.
_PHRASE_CEILING = 0.88


def stage_from_title(title: str | None) -> str | None:
    if not title:
        return None
    return next((stage for pattern, stage in _TITLE_TO_STAGE if pattern.search(title)), None)


@dataclass(frozen=True, slots=True)
class HeuristicRung:
    costly: bool = False

    def extract(self, msg: CandidateMessage, ctx: LadderContext) -> Extraction | None:
        sender_domain = domain_of_address(msg.headers.sender)
        ats_domains = ctx.registry.ats_domains

        if msg.invite is not None:
            invite = msg.invite
            return Extraction(
                intent="interview_cancelled" if invite.cancelled else "interview_invite",
                confidence=_CANCELLED_CONFIDENCE if invite.cancelled else _INVITE_CONFIDENCE,
                rung=2,
                stage_hint=stage_from_title(invite.summary),
                company=company_from_domain(invite.organiser, ats_domains),
            )

        # An ATS sender has already been through rung 1, which tries the same
        # vocabulary; and a practice site sells using the words of hiring, so a
        # "coding challenge" promotion is not a take-home.
        if ctx.registry.is_ats(sender_domain) or in_list(sender_domain, LEARNING_PLATFORMS):
            return None

        phrase = match_phrase(f"{msg.headers.subject}\n{msg.text}")
        if phrase is None:
            return None
        return Extraction(
            intent=phrase.intent,
            confidence=min(phrase.confidence, _PHRASE_CEILING),
            rung=2,
            company=company_from_display_name(msg.headers.sender)
            or company_from_domain(sender_domain, ats_domains),
        )
