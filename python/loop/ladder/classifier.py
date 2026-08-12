"""The cheap filter in front of the ladder.

"A cheap, deterministic filter whose only job is to protect the expensive rungs.
It MUST be biased towards recall: dropping a real application is invisible and
unrecoverable, while passing junk through costs 4 ms at rung 1."

Every branch below is one line of Engineering Spec §07, kept in the same order
so the two can be read side by side.
"""

import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Literal

from loop.domain import domain_of_address
from loop.domain.messages import RawMessage
from loop.domain.thresholds import CLASSIFIER_CHEAP_ONLY, CLASSIFIER_PASS

from .domains import BULK_WHITELIST, MEETING_HOSTS, PERSONAL_MAIL, SOCIAL_NOISE, in_list

Outcome = Literal["pass", "cheap_only", "drop"]

# How much of the body counts as the opening.
_OPENING_CHARS = 400

# Two vocabularies, not one.
#
# The original single regex included "selezione", "posizione" and "offerta",
# which in Italian are only job words in a job context: a fashion retailer's "la
# selezione in saldo", an estate agent's "posizione centrale" and any shop's
# "offerta" all matched it. Two thirds of everything that reached the extraction
# ladder in a real twelve-month mailbox was mail of exactly that kind.
#
# So the unambiguous words score on their own, and the ambiguous ones only count
# when something else already suggests this is about work. Recall is still the
# bias — a weak word plus any other signal is enough — but a weak word alone no
# longer is.
_STRONG_KEYWORDS = re.compile(
    r"candidatur|candidacy|\bapplication\b|applying|colloqui|interview|recruit|assunzione"
    r"|hiring|\bcurriculum\b|\bCV\b|risorse umane|talent acquisition|job offer"
    r"|offerta di lavoro|proposta di assunzione|processo di selezione",
    re.IGNORECASE,
)

_WEAK_KEYWORDS = re.compile(
    r"posizione|selezione|offerta|\brole\b|vacancy|talent|opportunit", re.IGNORECASE
)

# Mail from a job platform that is not about a job.
#
# LinkedIn is whitelisted past the bulk penalty because its application
# confirmations are bulk-flagged exactly like its alerts — but that waiver was
# applied to everything it sends, so profile views, invitation accepts, birthday
# nudges and security notices all sailed through. In this mailbox that was 186
# messages, every one of which became a review item asking a human to classify
# "your profile appeared in 8 searches".
_PLATFORM_NOISE = re.compile(
    "|".join(
        (
            # profile and network activity
            "profilo è apparso",
            r"appeared in \d+ search",
            "persone ti hanno notato",
            "ha accettato il tuo invito",
            "accepted your invitation",
            "inizia una conversazione con",
            "hanno aggiornamenti per te",
            "fai le congratulazioni",
            "congratulate",
            "ha aggiunto una reazione",
            "vedi i collegamenti",
            "vorrei collegarmi",
            "voglio collegarmi",
            r"hai \d+ nuov(?:o|i) (?:invito|inviti|messaggi)",
            "nuovo invito",
            "sent you a message",
            "ti ha inviato un messaggio",
            # alerts and account admin
            "avvisi? di offerte di lavoro",
            "job alert",
            "abbiamo disattivato",
            "sblocca informazioni",
            "verifica il tuo nuovo dispositivo",
            "livello di protezione",
            "two[- ]factor",
            "autenticazione a due fattori",
            "terms of service",
            "termini di servizio",
            "condizioni d'uso",
            "newsletter",
            "webinar",
            "unsubscribe preferences",
            # marketplace noise that borrows the vocabulary
            "saldi",
            "in saldo",
            "spedizione gratuita",
            "sconto",
        )
    ),
    re.IGNORECASE,
)

_NO_REPLY = re.compile(r"^(?:no[-._]?reply|do[-._]?not[-._]?reply|noreply)@", re.IGNORECASE)
_BULK_PRECEDENCE = re.compile(r"bulk|list", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ClassifierContext:
    """What the classifier knows about this user beyond the message itself."""

    ats_domains: tuple[str, ...] = ()
    # Domains of companies this user already has an application at.
    company_domains: AbstractSet[str] = frozenset()
    # Threads already attached to an application.
    known_threads: AbstractSet[str] = frozenset()
    # Newsletter senders learned from previous drops.
    known_newsletters: AbstractSet[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Classification:
    score: int
    outcome: Outcome
    reasons: tuple[str, ...]


@dataclass(slots=True)
class _Scorecard:
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, points: int, why: str) -> None:
        self.score += points
        self.reasons.append(f"{points:+d} {why}")

    def note(self, why: str) -> None:
        self.reasons.append(why)


def classify(msg: RawMessage, ctx: ClassifierContext) -> Classification:
    card = _Scorecard()
    sender_domain = domain_of_address(msg.headers.sender)
    haystack = f"{msg.headers.subject}\n{msg.text[:_OPENING_CHARS]}"

    strong = bool(_STRONG_KEYWORDS.search(haystack))
    weak = not strong and bool(_WEAK_KEYWORDS.search(haystack))
    # Noise is judged on the subject alone: a LinkedIn footer mentions searches
    # and alerts on every message it ever sends, including the real ones.
    noise = bool(_PLATFORM_NOISE.search(msg.headers.subject))

    is_ats = in_list(sender_domain, ctx.ats_domains)
    is_known_company = (
        msg.headers.list_unsubscribe is None
        and sender_domain is not None
        and sender_domain in ctx.company_domains
    )
    on_known_thread = msg.thread_id is not None and msg.thread_id in ctx.known_threads
    has_meeting_link = any(host in msg.text for host in MEETING_HOSTS) and not in_list(
        sender_domain, PERSONAL_MAIL
    )
    has_invite = msg.invite is not None or has_meeting_link

    if is_ats:
        card.add(3, "sender is a known ATS vendor")
    if is_known_company:
        card.add(3, "direct mail from a company already in the pipeline")

    if strong:
        card.add(2, "subject or opening names an application unambiguously")
    elif weak and (is_ats or is_known_company or on_known_thread or has_invite):
        card.add(2, "ambiguous vocabulary, corroborated by another signal")
    elif weak:
        card.add(1, "ambiguous vocabulary alone — enough to look at, not enough to trust")

    if on_known_thread:
        card.add(2, "reply on a thread already attached to an application")

    if has_invite:
        card.add(
            2,
            "carries a calendar invite"
            if msg.invite
            else "meeting link from a business domain",
        )

    _apply_penalties(
        card,
        msg,
        sender_domain,
        noise=noise,
        keyword_hit=strong or weak,
        known_newsletters=ctx.known_newsletters,
    )

    return Classification(card.score, _outcome(card.score), tuple(card.reasons))


def _apply_penalties(
    card: _Scorecard,
    msg: RawMessage,
    sender_domain: str | None,
    *,
    noise: bool,
    keyword_hit: bool,
    known_newsletters: AbstractSet[str],
) -> None:
    bulk = (
        bool(_BULK_PRECEDENCE.search(msg.headers.precedence or ""))
        or msg.headers.list_id is not None
        or (sender_domain is not None and sender_domain in known_newsletters)
    )
    # The waiver is for their *confirmations*, which is what §07 says. Extending
    # it to every notification the platform emits is what buried the review
    # queue.
    waived = in_list(sender_domain, BULK_WHITELIST) and not noise
    if bulk and not waived:
        card.add(-4, "bulk mail")
    elif bulk:
        card.note(
            "bulk penalty waived: LinkedIn/Indeed confirmations look exactly like their alerts"
        )

    # Platform housekeeping is never an application, whoever sent it.
    if noise:
        card.add(-4, "platform notification, not an application")

    address = msg.headers.sender.rsplit("<", 1)[-1]
    if _NO_REPLY.match(address) and not keyword_hit:
        card.add(-3, "no-reply sender with no vocabulary hit")

    if in_list(sender_domain, SOCIAL_NOISE):
        card.add(-2, "social or developer notification")


def _outcome(score: int) -> Outcome:
    if score >= CLASSIFIER_PASS:
        return "pass"
    if score >= CLASSIFIER_CHEAP_ONLY:
        return "cheap_only"
    return "drop"
