"""The shapes that travel between services.

Ported from `packages/domain/src/messages.ts`. One difference throughout:
timestamps are `datetime`, not ISO strings. The TypeScript carried strings
because they are queue payloads and JSON has no date type; Python has one, and
keeping the parse at the edge means nothing downstream re-parses a string it
already parsed. Serialisation happens once, at the queue boundary.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .types import Channel, EventType, Rung, WorkMode

Intent = Literal[
    "applied",
    "acknowledged",
    "schedule_screening",
    "interview_invite",
    "interview_cancelled",
    "take_home",
    "rejected",
    "offer",
    "negotiation",
    # "This vendor sent this, and it is not about an application" — webinars,
    # job alerts, newsletters. Without it those fall through to the model,
    # which is asked to guess about mail a rule recognises perfectly well.
    "other",
    "unclear",
]

Language = Literal["it", "en", "other"]

InviteStatus = Literal["confirmed", "cancelled", "tentative"]
InviteMethod = Literal["REQUEST", "CANCEL", "REPLY", "PUBLISH"]


@dataclass(frozen=True, slots=True)
class MessageHeaders:
    message_id: str
    # The `From` header, display name and all. `from` is a Python keyword and
    # `from_` reads badly everywhere it is used, so the mapping happens once,
    # here and in whatever parses a message.
    sender: str
    subject: str
    date: str
    to: tuple[str, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    list_id: str | None = None
    list_unsubscribe: str | None = None
    precedence: str | None = None
    auto_submitted: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarInvite:
    uid: str
    summary: str | None
    starts_at: datetime
    ends_at: datetime | None = None
    location: str | None = None
    organiser: str | None = None
    attendees: tuple[str, ...] = ()
    status: InviteStatus = "confirmed"
    method: InviteMethod | None = None

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled" or self.method == "CANCEL"


@dataclass(frozen=True, slots=True)
class RawMessage:
    """connector → classifier."""

    user_id: str
    mailbox_id: str
    provider_message_id: str
    received_at: datetime
    headers: MessageHeaders
    # HTML already reduced to text, quoted history dropped, capped at 6 000.
    text: str
    body_sha256: str
    thread_id: str | None = None
    invite: CalendarInvite | None = None
    # True when this arrived from a backfill rather than a live push.
    backfill: bool = False


@dataclass(frozen=True, slots=True)
class CandidateMessage:
    """classifier → extractor."""

    message: RawMessage
    score: int
    # Score 1–2: rungs 1 and 2 only, never the model.
    cheap_only: bool
    reasons: tuple[str, ...] = ()

    @property
    def headers(self) -> MessageHeaders:
        return self.message.headers

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def thread_id(self) -> str | None:
        return self.message.thread_id

    @property
    def invite(self) -> CalendarInvite | None:
        return self.message.invite


@dataclass(frozen=True, slots=True)
class Comp:
    currency: str
    min_minor: int | None = None
    max_minor: int | None = None


@dataclass(frozen=True, slots=True)
class Signal:
    """extractor → resolver. One extracted, structured observation.

    Deliberately not attached to an application: identity is the resolver's
    job, and keeping the boundary sharp is what stops two components from both
    half-guessing which application a message belongs to.
    """

    user_id: str
    mailbox_id: str
    provider_message_id: str
    evidence_ref: str
    intent: Intent
    occurred_at: datetime
    confidence: float
    rung: Rung
    language: Language
    thread_id: str | None = None
    sender_domain: str | None = None
    company: str | None = None
    # As written in the message — this is what the interface shows.
    role: str | None = None
    # Seniority lifted out, abbreviations expanded, location stripped. The
    # comparison key the resolver embeds, never the display string.
    role_normalised: str | None = None
    stage_hint: str | None = None
    deadline: str | None = None
    comp: Comp | None = None
    decide_by: datetime | None = None
    ats_vendor: str | None = None
    channel: Channel | None = None
    posting_url: str | None = None
    location: str | None = None
    work_mode: WorkMode | None = None
    invite: CalendarInvite | None = None
    # ≤280 chars, redacted. Only ever set when this is heading for review.
    excerpt: str | None = None
    # The application this message already belongs to, when the thread says so.
    application_hint: str | None = None


# Keys stripped before a payload is dead-lettered. Text travels in the queue
# while a message is in flight and the ack deletes the row; the dead-letter
# path is the only one where a body would persist indefinitely.
BODY_KEYS: frozenset[str] = frozenset({"text", "body", "html", "snippet", "excerpt"})


def strip_bodies(payload: object) -> object:
    if isinstance(payload, list):
        return [strip_bodies(v) for v in payload]
    if isinstance(payload, dict):
        return {
            k: "[stripped]" if k in BODY_KEYS else strip_bodies(v) for k, v in payload.items()
        }
    return payload


@dataclass(frozen=True, slots=True)
class PendingNotification:
    """nudge → notifier."""

    user_id: str
    suggestion_key: str
    rule: str
    title: str
    body: str
    url: str
    # Only the deadline rule sets this.
    bypasses_budget: bool = False


@dataclass(frozen=True, slots=True)
class EventSource:
    """A provenance row to attach when a signal introduced a new channel."""

    channel: Channel
    posting_url: str | None = None
    ats_vendor: str | None = None
    is_first_touch: bool = False


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """resolver → pipeline. The only shape that becomes an event."""

    user_id: str
    application_id: str
    type: EventType
    occurred_at: datetime
    confidence: float
    from_stage: str | None = None
    to_stage: str | None = None
    evidence_ref: str | None = None
    rung: Rung | None = None
    payload: dict[str, object] = field(default_factory=dict)
    source: EventSource | None = None
    # Set on replay so the pipeline can skip re-notifying.
    silent: bool = False
