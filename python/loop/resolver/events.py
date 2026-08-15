"""Intent → event.

The closed set of fourteen event types is the only vocabulary the pipeline
accepts, so this is where an extracted observation becomes a claim about the
world. Nothing here decides *which* application the claim is about; that is
settled before this runs.

This is the third and last place the `technical` default lived. Rung 2 answered
it for a titleless calendar invite, `stage_for_intent` answered it for the
intent, and the TypeScript resolver answered it again on four separate lines —
so removing it from the ladder alone would have put it straight back. A titleless
invitation now advances the phase and leaves the round unnamed, which is the
claim the evidence actually supports.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loop.domain.messages import EventSource, PendingEvent, Signal
from loop.domain.normalise import normalise_role
from loop.domain.stages import UNSPECIFIED_INTERVIEW
from loop.domain.types import EventType, WorkMode


@dataclass(frozen=True, slots=True)
class RoleFacts:
    """What a signal says about the job, beyond its title.

    Read in two places that must not drift: the row the resolver inserts when it
    creates an application, and the payload of every event it appends. The
    reference derived them for the row only, so `rebuild_all` blanked a
    `seniority` nothing in the log could put back.
    """

    seniority: str | None = None
    location: str | None = None
    work_mode: WorkMode | None = None


def role_facts(signal: Signal) -> RoleFacts:
    normalised = normalise_role(signal.role) if signal.role else None
    if normalised is None:
        return RoleFacts(location=signal.location, work_mode=signal.work_mode)
    return RoleFacts(
        seniority=normalised.seniority,
        location=signal.location or normalised.location,
        work_mode=signal.work_mode or normalised.work_mode,
    )


def events_for_signal(signal: Signal, application_id: str) -> list[PendingEvent]:
    builder = _Builder(signal, application_id)

    match signal.intent:
        case "applied":
            return [builder.event("applied", {"posting_url": signal.posting_url}, "applied")]

        case "acknowledged":
            return [
                builder.event("acknowledged", {"ats_vendor": signal.ats_vendor}, "acknowledged")
            ]

        case "schedule_screening":
            return [
                builder.event(
                    "stage_advanced",
                    {"note": "availability requested"},
                    signal.stage_hint or "recruiter_reachout",
                )
            ]

        case "interview_invite":
            return [builder.interview("confirmed")]

        case "interview_cancelled":
            # The claim is withdrawn, but the reversal is never automatic: a
            # cancelled round can mean "rescheduling" or "it is over", and only
            # the user knows which. The pipeline drops the stage claim; the
            # resolver raises a card.
            return [builder.interview("cancelled")] if signal.invite else []

        case "take_home":
            events = [builder.event("stage_advanced", {}, "take_home")]
            if signal.deadline:
                events.append(
                    builder.event(
                        "deadline_set",
                        {
                            "kind": "take_home",
                            "due_at": signal.deadline,
                            "url": signal.posting_url,
                            "source": signal.ats_vendor or "gmail",
                        },
                    )
                )
            return events

        case "rejected":
            return [builder.event("rejected", {"after_stage": signal.stage_hint})]

        case "offer":
            return [
                builder.event(
                    "offer_received",
                    {
                        "min_minor": signal.comp.min_minor if signal.comp else None,
                        "max_minor": signal.comp.max_minor if signal.comp else None,
                        "currency": signal.comp.currency if signal.comp else "EUR",
                        "decide_by": signal.decide_by,
                    },
                    "offer",
                )
            ]

        case "negotiation":
            return [
                builder.event(
                    "offer_negotiated",
                    {
                        "min_minor": signal.comp.min_minor if signal.comp else None,
                        "currency": signal.comp.currency if signal.comp else "EUR",
                    },
                    "negotiating",
                )
            ]

        case _:
            # `other` and `unclear` produce nothing. A rung that reached here
            # without a claim has already abstained, and inventing a stage
            # change out of silence is the failure this system exists to avoid.
            return []


class _Builder:
    """The parts of an event that do not depend on the intent."""

    __slots__ = ("_application_id", "_facts", "_signal")

    def __init__(self, signal: Signal, application_id: str) -> None:
        self._signal = signal
        self._application_id = application_id
        self._facts = role_facts(signal)

    def event(
        self,
        type_: EventType,
        payload: Mapping[str, Any] | None = None,
        to_stage: str | None = None,
    ) -> PendingEvent:
        signal = self._signal
        return PendingEvent(
            user_id=signal.user_id,
            application_id=self._application_id,
            type=type_,
            occurred_at=signal.occurred_at,
            confidence=signal.confidence,
            to_stage=to_stage,
            evidence_ref=signal.evidence_ref,
            rung=signal.rung,
            payload={
                "thread_id": signal.thread_id,
                "role_title": signal.role,
                # The same three facts the row gets, so the row stays derivable.
                # The reference put them on the row alone, and `seniority` in
                # particular came only from `normalise_role` — so a rebuild
                # blanked a column nothing in the log could put back.
                "seniority": self._facts.seniority,
                "location": self._facts.location,
                "work_mode": self._facts.work_mode,
                "channel": signal.channel,
                **(payload or {}),
            },
            source=self._source(),
        )

    def interview(self, status: str) -> PendingEvent:
        """A scheduled or cancelled round.

        A scheduled interview carries a stage: whichever round the invitation
        named, or `interview` when it named none. That is not the old default
        under another name — `technical` asserted which round it was, and this
        asserts only that the process has reached interviewing, which is what an
        invitation with an unreadable title actually proves.

        A cancellation carries no stage at all. The fold reads the payload's
        `stage` for this event type, so leaving it out is what makes "the claim
        is withdrawn" true rather than merely commented: a cancelled round must
        not advance anything. Whether the application moved *back* is a question
        only the user can answer, and the resolver raises a card to ask it.
        """
        signal = self._signal
        cancelled = status == "cancelled"
        payload: dict[str, Any] = {"status": status}
        if not cancelled:
            payload["stage"] = signal.stage_hint or UNSPECIFIED_INTERVIEW
        if signal.invite:
            payload |= {
                "starts_at": signal.invite.starts_at,
                "ends_at": signal.invite.ends_at,
                "location": signal.invite.location,
                "calendar_event_id": signal.invite.uid,
            }
        return self.event(
            "interview_scheduled", payload, None if cancelled else payload["stage"]
        )

    def _source(self) -> EventSource | None:
        """Provenance, attached only when the signal introduced a channel."""
        signal = self._signal
        if not signal.channel:
            return None
        return EventSource(
            channel=signal.channel,
            posting_url=signal.posting_url,
            ats_vendor=signal.ats_vendor,
            # Exactly one first touch per application, and only an `applied`
            # signal can claim it — every channel statistic depends on that.
            is_first_touch=signal.intent == "applied",
        )
