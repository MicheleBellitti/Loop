"""Turning a rung's reading into the one shape the resolver accepts.

A signal is "one extracted, structured observation from one source message — not
yet attached to an application", so nothing here decides identity. That is the
resolver's job, and keeping the boundary sharp is what stops two components from
both half-guessing which application a message belongs to.
"""

import re

from loop.domain import (
    detect_language,
    domain_of_address,
    excerpt,
    matches_domain_suffix,
    normalise_role,
)
from loop.domain.messages import CandidateMessage, Intent, Signal
from loop.domain.types import Channel

from .contracts import Extraction, LadderContext
from .role import role_from_body

_POSTING_URL = re.compile(
    r"""https?://[^\s<>"')]*/(?:jobs?|careers?|posizioni|vacanc\w*|opportunit\w*)/[^\s<>"')]*""",
    re.IGNORECASE,
)

# The stage an intent implies when nothing more specific was extracted.
#
# `interview_invite` is deliberately absent. An invitation whose title named no
# stage is evidence that an interview is scheduled and no evidence at all of
# which kind, and answering "technical" to that question is how the pipeline
# came to show a column of identical stages. The claim the evidence supports is
# the phase, and the resolver derives that from the intent.
_STAGE_FOR_INTENT: dict[Intent, str] = {
    "applied": "applied",
    "acknowledged": "acknowledged",
    "schedule_screening": "recruiter_reachout",
    "take_home": "take_home",
    "offer": "offer",
    "negotiation": "negotiating",
}


def stage_for_intent(intent: Intent) -> str | None:
    return _STAGE_FOR_INTENT.get(intent)


def channel_for_vendor(vendor: str | None, sender_domain: str | None) -> Channel | None:
    """Channel, attributed from the sender.

    `referral` is never inferred: a referral is something a human tells us, and
    guessing one would quietly flatter the channel statistics the whole feature
    exists to keep honest.
    """
    if vendor == "linkedin":
        return "linkedin"
    if vendor == "indeed":
        return "indeed"
    if vendor:
        # An ATS means you applied on their site.
        return "career_page"
    # Gmail specifically, and not every personal provider: what this excludes is
    # the user's own address, not the possibility of a recruiter writing from a
    # personal mailbox.
    if sender_domain and not matches_domain_suffix(sender_domain, "gmail.com"):
        return "recruiter"
    return None


def build_signal(msg: CandidateMessage, reading: Extraction, ctx: LadderContext) -> Signal:
    sender_domain = domain_of_address(msg.headers.sender)
    vendor = ctx.registry.vendor_for(sender_domain)
    haystack = f"{msg.headers.subject}\n{msg.text}"

    # Whatever rung produced this reading, the job title may still be sitting in
    # the body — a calendar invite says "Interview with Prima" in its subject
    # and names the role in the description. Recovering it here means every rung
    # benefits, and the resolver gets something to match on instead of the
    # placeholder that made every roleless signal look like a new application.
    role = reading.role or role_from_body(haystack)
    normalised = normalise_role(role) if role else None
    posting = _POSTING_URL.search(msg.text)

    return Signal(
        user_id=msg.message.user_id,
        mailbox_id=msg.message.mailbox_id,
        provider_message_id=msg.message.provider_message_id,
        thread_id=msg.thread_id,
        evidence_ref=msg.message.provider_message_id,
        sender_domain=sender_domain,
        intent=reading.intent,
        company=reading.company,
        # The original, because "Senior Backend Engineer" is what the user
        # applied for and what they will recognise in a list. The normalised
        # form travels beside it for the resolver to embed — conflating the two
        # put lower-cased strings on screen.
        role=role,
        role_normalised=normalised.role if normalised else None,
        stage_hint=reading.stage_hint or stage_for_intent(reading.intent),
        # A calendar invite is the most accurate `occurred_at` in the mailbox:
        # the event happened when the meeting is, not when the mail arrived.
        occurred_at=msg.invite.starts_at if msg.invite else msg.message.received_at,
        deadline=reading.deadline,
        comp=reading.comp,
        decide_by=reading.decide_by,
        language=detect_language(msg.text),
        confidence=reading.confidence,
        rung=reading.rung,
        ats_vendor=vendor,
        channel=channel_for_vendor(vendor, sender_domain),
        posting_url=posting.group(0) if posting else None,
        location=normalised.location if normalised else None,
        work_mode=normalised.work_mode if normalised else None,
        invite=msg.invite,
        # Carried on every signal, not only the ones already known to be
        # heading for review. The resolver raises its own review items when a
        # match is ambiguous, and in the TypeScript those arrived with a null
        # excerpt — a review card with nothing on it to judge by.
        excerpt=excerpt(f'"{msg.text}" — {msg.headers.sender}'),
        application_hint=_application_hint(msg, ctx),
    )


def _application_hint(msg: CandidateMessage, ctx: LadderContext) -> str | None:
    """The thread identity, which is the strongest and cheapest signal there is.

    Read for every rung rather than only for rung 2: a message's application does
    not depend on which rung managed to read its intent.
    """
    if not msg.thread_id:
        return None
    return ctx.thread_to_application.get(msg.thread_id)
