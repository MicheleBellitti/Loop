"""What the shapes look like on the queue.

The dataclasses in `messages.py` are how this codebase wants to think; JSON is
what the queue holds and what a TypeScript service on the other end of the same
queue reads. The translation lives here rather than in either of them, so the
dataclasses stay flat and the payloads stay compatible.

Two things this exists to get right.

**The event envelope is nested on the wire and flat in the dataclass.** A
`PendingEvent` travels as `{user_id, application_id, event: {…}}`, and four SQL
sites build it that way: `sweep_dormancy`, `sweep_dormancy_all`,
`mark_interviews_held` and the presumed-closed sweep all emit
`jsonb_build_object('event', jsonb_build_object(…))`. A decoder that expects the
flat shape drops every event the scheduler produces and dead-letters it after
five deliveries — which would look exactly like dormancy quietly not working.

**A key that is absent is not a key that is null.** `JSON.stringify` omits
`undefined` and keeps `null`, so the reference's payloads have `to_stage: null`
and no `from_stage` at all. Emitting both as null, or dropping both, produces
payloads that differ from the reference's for no reason while the two share a
queue.
"""

from datetime import datetime
from typing import Any

from .messages import (
    CalendarInvite,
    CandidateMessage,
    Comp,
    EventSource,
    MessageHeaders,
    PendingEvent,
    PendingNotification,
    RawMessage,
    Signal,
)

Json = dict[str, Any]


def _moment(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _required_moment(value: Any) -> datetime:
    moment = _moment(value)
    if moment is None:
        raise ValueError("a timestamp is required here")
    return moment


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ── calendar invites ────────────────────────────────────────────────────────


def encode_invite(invite: CalendarInvite) -> Json:
    return {
        "uid": invite.uid,
        "summary": invite.summary,
        "starts_at": _iso(invite.starts_at),
        "ends_at": _iso(invite.ends_at),
        "location": invite.location,
        "organiser": invite.organiser,
        "attendees": list(invite.attendees),
        "status": invite.status,
        "method": invite.method,
    }


def decode_invite(row: Json) -> CalendarInvite:
    return CalendarInvite(
        uid=row["uid"],
        summary=row.get("summary"),
        starts_at=_required_moment(row["starts_at"]),
        ends_at=_moment(row.get("ends_at")),
        location=row.get("location"),
        organiser=row.get("organiser"),
        attendees=tuple(row.get("attendees") or ()),
        status=row.get("status") or "confirmed",
        method=row.get("method"),
    )


# ── connector → classifier → extractor ──────────────────────────────────────


def encode_headers(headers: MessageHeaders) -> Json:
    """Every key `decode_headers` reads, and the same ones `normalise.ts` writes.

    `in_reply_to`, `references` and `auto_submitted` were decoded and never
    encoded, so the connector read three headers off the message and the
    classifier one hop later could not see any of them. Nothing in the Python
    reads them yet, which is what kept it quiet; the TypeScript on the other end
    of the same queue does put them on the wire, and a decoder that silently
    fills in a default for a key the encoder simply forgot is the shape of a bug
    that only appears once something starts depending on it.
    """
    return {
        "message_id": headers.message_id,
        "from": headers.sender,
        "to": list(headers.to),
        "subject": headers.subject,
        "date": headers.date,
        "in_reply_to": headers.in_reply_to,
        "references": list(headers.references),
        "list_id": headers.list_id,
        "list_unsubscribe": headers.list_unsubscribe,
        "precedence": headers.precedence,
        "auto_submitted": headers.auto_submitted,
    }


def decode_headers(row: Json) -> MessageHeaders:
    return MessageHeaders(
        message_id=row.get("message_id", ""),
        sender=row.get("from", ""),
        subject=row.get("subject", ""),
        date=row.get("date", ""),
        to=tuple(row.get("to") or ()),
        in_reply_to=row.get("in_reply_to"),
        references=tuple(row.get("references") or ()),
        list_id=row.get("list_id"),
        list_unsubscribe=row.get("list_unsubscribe"),
        precedence=row.get("precedence"),
        auto_submitted=row.get("auto_submitted"),
    )


def encode_raw_message(message: RawMessage) -> Json:
    payload: Json = {
        "user_id": message.user_id,
        "mailbox_id": message.mailbox_id,
        "provider_message_id": message.provider_message_id,
        "thread_id": message.thread_id,
        "received_at": _iso(message.received_at),
        "headers": encode_headers(message.headers),
        "text": message.text,
        "body_sha256": message.body_sha256,
        "invite": encode_invite(message.invite) if message.invite else None,
    }
    if message.backfill:
        payload["backfill"] = True
    return payload


def decode_raw_message(row: Json) -> RawMessage:
    invite = row.get("invite")
    return RawMessage(
        user_id=row["user_id"],
        mailbox_id=row["mailbox_id"],
        provider_message_id=row["provider_message_id"],
        thread_id=row.get("thread_id"),
        received_at=_required_moment(row["received_at"]),
        headers=decode_headers(row.get("headers") or {}),
        text=row.get("text", ""),
        body_sha256=row.get("body_sha256", ""),
        invite=decode_invite(invite) if invite else None,
        backfill=bool(row.get("backfill")),
    )


def encode_candidate_message(candidate: CandidateMessage) -> Json:
    """Flat, because the reference spreads the message rather than nesting it."""
    return {
        **encode_raw_message(candidate.message),
        "score": candidate.score,
        "cheap_only": candidate.cheap_only,
        "reasons": list(candidate.reasons),
    }


def decode_candidate_message(row: Json) -> CandidateMessage:
    return CandidateMessage(
        message=decode_raw_message(row),
        score=int(row.get("score", 0)),
        cheap_only=bool(row.get("cheap_only")),
        reasons=tuple(row.get("reasons") or ()),
    )


# ── extractor → resolver ────────────────────────────────────────────────────


def encode_signal(signal: Signal) -> Json:
    return {
        "user_id": signal.user_id,
        "mailbox_id": signal.mailbox_id,
        "provider_message_id": signal.provider_message_id,
        "thread_id": signal.thread_id,
        "evidence_ref": signal.evidence_ref,
        "sender_domain": signal.sender_domain,
        "intent": signal.intent,
        "company": signal.company,
        "role": signal.role,
        "role_normalised": signal.role_normalised,
        "stage_hint": signal.stage_hint,
        "occurred_at": _iso(signal.occurred_at),
        "deadline": signal.deadline,
        "comp": _encode_comp(signal.comp),
        "decide_by": _iso(signal.decide_by),
        "language": signal.language,
        "confidence": signal.confidence,
        "rung": signal.rung,
        "ats_vendor": signal.ats_vendor,
        "channel": signal.channel,
        "posting_url": signal.posting_url,
        "location": signal.location,
        "work_mode": signal.work_mode,
        "invite": encode_invite(signal.invite) if signal.invite else None,
        "excerpt": signal.excerpt,
        "application_hint": signal.application_hint,
    }


def decode_signal(row: Json) -> Signal:
    invite, comp = row.get("invite"), row.get("comp")
    return Signal(
        user_id=row["user_id"],
        mailbox_id=row["mailbox_id"],
        provider_message_id=row["provider_message_id"],
        evidence_ref=row["evidence_ref"],
        intent=row["intent"],
        occurred_at=_required_moment(row["occurred_at"]),
        confidence=float(row["confidence"]),
        rung=row["rung"],
        language=row.get("language", "other"),
        thread_id=row.get("thread_id"),
        sender_domain=row.get("sender_domain"),
        company=row.get("company"),
        role=row.get("role"),
        role_normalised=row.get("role_normalised"),
        stage_hint=row.get("stage_hint"),
        deadline=row.get("deadline"),
        comp=_decode_comp(comp) if comp else None,
        decide_by=_moment(row.get("decide_by")),
        ats_vendor=row.get("ats_vendor"),
        channel=row.get("channel"),
        posting_url=row.get("posting_url"),
        location=row.get("location"),
        work_mode=row.get("work_mode"),
        invite=decode_invite(invite) if invite else None,
        excerpt=row.get("excerpt"),
        application_hint=row.get("application_hint"),
    )


def _encode_comp(comp: Comp | None) -> Json | None:
    if comp is None:
        return None
    return {
        "min_minor": comp.min_minor,
        "max_minor": comp.max_minor,
        "currency": comp.currency,
    }


def _decode_comp(row: Json) -> Comp:
    return Comp(
        currency=row.get("currency", "EUR"),
        min_minor=row.get("min_minor"),
        max_minor=row.get("max_minor"),
    )


# ── resolver → pipeline ─────────────────────────────────────────────────────


def encode_pending_event(pending: PendingEvent) -> Json:
    event: Json = {
        "type": pending.type,
        "occurred_at": _iso(pending.occurred_at),
        "confidence": pending.confidence,
        "to_stage": pending.to_stage,
        "payload": pending.payload,
        "evidence_ref": pending.evidence_ref,
        "rung": pending.rung,
    }
    if pending.from_stage is not None:
        event["from_stage"] = pending.from_stage

    envelope: Json = {
        "user_id": pending.user_id,
        "application_id": pending.application_id,
        "event": event,
    }
    if pending.source is not None:
        envelope["source"] = {
            "channel": pending.source.channel,
            "posting_url": pending.source.posting_url,
            "ats_vendor": pending.source.ats_vendor,
            "is_first_touch": pending.source.is_first_touch,
        }
    if pending.silent:
        envelope["silent"] = True
    return envelope


def decode_pending_event(row: Json) -> PendingEvent:
    """Accepts the nested envelope, which is the only shape on the wire.

    The scheduler's events arrive this way and so do the resolver's; a flat
    reading would quietly drop the former.
    """
    event = row.get("event")
    if not isinstance(event, dict):
        raise ValueError("a pending event needs a nested `event` object")
    source = row.get("source")
    return PendingEvent(
        user_id=row["user_id"],
        application_id=row["application_id"],
        type=event["type"],
        occurred_at=_required_moment(event["occurred_at"]),
        confidence=float(event.get("confidence", 1.0)),
        from_stage=event.get("from_stage"),
        to_stage=event.get("to_stage"),
        evidence_ref=event.get("evidence_ref"),
        rung=event.get("rung"),
        payload=event.get("payload") or {},
        source=_decode_source(source) if source else None,
        silent=bool(row.get("silent")),
    )


# ── nudge → notifier ────────────────────────────────────────────────────────


def encode_pending_notification(notification: PendingNotification) -> Json:
    """Flat and snake_case, unlike the suggestion payload it was built from.

    The two shapes travel together and disagree on purpose: this one is a queue
    message between two services, while `suggestions.payload` is written
    camelCase because the API spreads it into a response the browser reads.
    """
    return {
        "user_id": notification.user_id,
        "suggestion_key": notification.suggestion_key,
        "rule": notification.rule,
        "title": notification.title,
        "body": notification.body,
        "url": notification.url,
        "bypasses_budget": notification.bypasses_budget,
    }


def decode_pending_notification(row: Json) -> PendingNotification:
    return PendingNotification(
        user_id=row["user_id"],
        suggestion_key=row["suggestion_key"],
        rule=row["rule"],
        title=row["title"],
        body=row["body"],
        url=row["url"],
        bypasses_budget=bool(row.get("bypasses_budget")),
    )


def _decode_source(row: Json) -> EventSource:
    return EventSource(
        channel=row["channel"],
        posting_url=row.get("posting_url"),
        ats_vendor=row.get("ats_vendor"),
        is_first_touch=bool(row.get("is_first_touch")),
    )
