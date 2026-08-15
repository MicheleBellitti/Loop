"""A Gmail message, as this system wants to think about it.

Pure: no network, no database, no clock. That is what lets the whole of the
hardest reading in the connector — which part of a MIME tree is the message,
which is the invitation, what an `.ics` actually says — be tested against real
anonymised mail in a tenth of a second.

Two things here are more careful than they look. The body is chosen from the
tree rather than taken from the first part, because a real recruiting mail is
`multipart/alternative` and the plain-text half is very often a stub that says
"this message requires HTML". And the `.ics` is unescaped, because RFC 5545
escapes commas and semicolons in every text value — the reference did not, and
the location of every interview in the database currently reads
`Dinova\\, Via Francesco Zanardi\\, 51`.
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Final

from loop.domain.messages import (
    CalendarInvite,
    InviteMethod,
    InviteStatus,
    MessageHeaders,
    RawMessage,
)
from loop.domain.preprocess import normalise_message
from loop.google.client import base64url_decode

# The cap the rest of the system assumes. A recruiting mail says what it has to
# say in the first screen; past this is signature, legal boilerplate and the
# forty messages of a quoted thread.
MAX_BODY_CHARS: Final = 6000

_HEADER_NAMES = {
    "message-id": "message_id",
    "from": "sender",
    "subject": "subject",
    "date": "date",
    "in-reply-to": "in_reply_to",
    "list-id": "list_id",
    "list-unsubscribe": "list_unsubscribe",
    "precedence": "precedence",
    "auto-submitted": "auto_submitted",
}

# `\,` `\;` `\\` and `\n` — the four escapes RFC 5545 defines for a text value.
_ICS_ESCAPES = re.compile(r"\\([,;\\nN])")

_ICS_LINE = re.compile(r"^(?P<name>[A-Za-z0-9-]+)(?P<params>;[^:]*)?:(?P<value>.*)$")

_STATUSES: dict[str, InviteStatus] = {
    "confirmed": "confirmed",
    "cancelled": "cancelled",
    "tentative": "tentative",
}
_METHODS: dict[str, InviteMethod] = {
    "REQUEST": "REQUEST",
    "CANCEL": "CANCEL",
    "REPLY": "REPLY",
    "PUBLISH": "PUBLISH",
}


def to_raw_message(
    message: dict[str, Any], *, user_id: str, mailbox_id: str, backfill: bool = False
) -> RawMessage:
    """The shape the classifier reads, from the shape Gmail returns."""
    parts = _walk(message.get("payload"))
    headers = _headers(message.get("payload"))
    body = _body(parts)
    received_at = _received_at(message, headers)

    return RawMessage(
        user_id=user_id,
        mailbox_id=mailbox_id,
        provider_message_id=message["id"],
        thread_id=message.get("threadId"),
        received_at=received_at,
        headers=headers,
        text=body,
        # Over the text this system kept, not over the bytes Gmail sent: what
        # it is for is noticing that the same message arrived twice, and two
        # deliveries of one message differ in their transport headers.
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        invite=_invite(parts),
        backfill=backfill,
    )


def _headers(payload: dict[str, Any] | None) -> MessageHeaders:
    raw: dict[str, str] = {}
    references: tuple[str, ...] = ()
    for header in (payload or {}).get("headers") or ():
        name = str(header.get("name", "")).lower()
        value = str(header.get("value", ""))
        if name == "references":
            references = tuple(value.split())
        elif name in _HEADER_NAMES:
            raw[_HEADER_NAMES[name]] = value
    return MessageHeaders(
        message_id=raw.get("message_id", ""),
        sender=raw.get("sender", ""),
        subject=raw.get("subject", ""),
        date=raw.get("date", ""),
        to=_addresses(payload),
        in_reply_to=raw.get("in_reply_to"),
        references=references,
        list_id=raw.get("list_id"),
        list_unsubscribe=raw.get("list_unsubscribe"),
        precedence=raw.get("precedence"),
        auto_submitted=raw.get("auto_submitted"),
    )


def _addresses(payload: dict[str, Any] | None) -> tuple[str, ...]:
    """Every recipient, which is how the extractor knows a reply is the user's."""
    found: list[str] = []
    for header in (payload or {}).get("headers") or ():
        if str(header.get("name", "")).lower() in ("to", "cc", "delivered-to"):
            found.extend(part.strip() for part in str(header.get("value", "")).split(","))
    return tuple(address for address in found if address)


def _body(parts: list[dict[str, Any]]) -> str:
    """Both halves of the message, handed to the reader that already exists.

    `normalise_message` is where the decision lives — HTML flattened when there
    is any, quoted history and signature stripped, whitespace collapsed — and it
    is the code the differential harness measured a thousand real messages
    against. Choosing between the parts here instead would put a second, unmeasured
    reading upstream of everything P1 validated.
    """
    return normalise_message(
        text=_decoded(parts, "text/plain") or None,
        html=_decoded(parts, "text/html") or None,
    ).text[:MAX_BODY_CHARS]


def _decoded(parts: list[dict[str, Any]], mime_type: str) -> str:
    for part in parts:
        if part.get("mimeType") != mime_type:
            continue
        data = (part.get("body") or {}).get("data")
        if data:
            return base64url_decode(data).decode("utf-8", errors="replace")
    return ""


def _received_at(message: dict[str, Any], headers: MessageHeaders) -> datetime:
    """Gmail's own timestamp, and the header only if it has none.

    `internalDate` is when Gmail accepted the message, which is a fact; the
    `Date` header is whatever the sender's machine believed, which on bulk mail
    is regularly hours out and occasionally years.
    """
    internal = message.get("internalDate")
    if internal:
        return datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
    if headers.date:
        from email.utils import parsedate_to_datetime

        try:
            parsed = parsedate_to_datetime(headers.date)
        except (TypeError, ValueError):
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _invite(parts: list[dict[str, Any]]) -> CalendarInvite | None:
    """The same predicate the client hydrates against. Change one, change both."""
    for part in parts:
        filename = (part.get("filename") or "").lower()
        if part.get("mimeType") != "text/calendar" and not filename.endswith(".ics"):
            continue
        data = (part.get("body") or {}).get("data")
        if data:
            return parse_ics(base64url_decode(data).decode("utf-8", errors="replace"))
    return None


def parse_ics(text: str) -> CalendarInvite | None:
    """Enough of RFC 5545 to read an interview invitation, and no more.

    Not a calendar library: this reads seven properties out of the first VEVENT
    and ignores recurrence, alarms, timezone definitions and attachments. An
    invitation to a job interview is a single event with a start, and the parts
    of the standard that exist for scheduling a fortnightly stand-up cost more
    to support than they would ever earn here.
    """
    event = _first_event(_unfolded(text))
    if event is None:
        return None

    start = _moment(event.get("dtstart"))
    if start is None:
        return None
    return CalendarInvite(
        uid=_text(event.get("uid")) or "",
        summary=_text(event.get("summary")),
        starts_at=start,
        ends_at=_moment(event.get("dtend")),
        location=_text(event.get("location")),
        organiser=_address(event.get("organizer")),
        attendees=tuple(a for a in (_address(v) for v in event.get("attendee", [])) if a),
        # `cancelled` is the one that matters as much as the invitation: an
        # interview called off is a fact about the application.
        status=_status(event.get("status")),
        method=_method(event.get("method")),
    )


def _status(entry: Any) -> InviteStatus:
    """Anything unrecognised is confirmed, which is what an invitation is."""
    return _STATUSES.get((_text(entry) or "").lower(), "confirmed")


def _method(entry: Any) -> InviteMethod | None:
    return _METHODS.get((_text(entry) or "").upper())


def _unfolded(text: str) -> list[str]:
    """RFC 5545 folds a long line by starting the next one with a space."""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _first_event(lines: list[str]) -> dict[str, Any] | None:
    """The first VEVENT, with the calendar-level METHOD carried into it."""
    event: dict[str, Any] | None = None
    method: str | None = None
    for line in lines:
        match = _ICS_LINE.match(line.strip())
        if match is None:
            continue
        name = match.group("name").upper()
        value = match.group("value")
        if name == "BEGIN" and value.upper() == "VEVENT":
            event = {}
            continue
        if name == "END" and value.upper() == "VEVENT":
            break
        if event is None:
            if name == "METHOD":
                method = value
            continue
        key = name.lower()
        entry = {"value": value, "params": match.group("params") or ""}
        if key == "attendee":
            event.setdefault("attendee", []).append(entry)
        else:
            event[key] = entry
    if event is not None and method and "method" not in event:
        event["method"] = {"value": method, "params": ""}
    return event


def _text(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    return _unescaped(entry["value"]) or None


def _unescaped(value: str) -> str:
    """`\\,` back to `,`, and the other three.

    Not cosmetic: without it every interview location in the database reads
    `Dinova\\, Via Francesco Zanardi\\, 51\\, 40131 Bologna`, which is what the
    reference stored and what is in there now.
    """
    return _ICS_ESCAPES.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _address(entry: Any) -> str | None:
    if isinstance(entry, list):
        return None
    value = _text(entry)
    if value is None:
        return None
    return value.removeprefix("mailto:").removeprefix("MAILTO:")


def _moment(entry: Any) -> datetime | None:
    """`20260827T163000Z`, or the same with a TZID, or a bare date.

    A date with no time is an all-day event, which for an interview means
    somebody's calendar client wrote the invitation badly. It is read as
    midnight UTC rather than dropped: the day is the useful part.
    """
    if not isinstance(entry, dict):
        return None
    value = entry["value"].strip()
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if "T" in value:
            local = datetime.strptime(value, "%Y%m%dT%H%M%S")
            return _in_zone(local, entry.get("params", ""))
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _in_zone(local: datetime, params: str) -> datetime:
    """A floating time in the zone the invitation named, or UTC.

    An unknown zone name is read as UTC rather than raising: a wrong hour on an
    interview is a bad reading, and no interview at all is a worse one.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    match = re.search(r"TZID=([^;:]+)", params)
    if not match:
        return local.replace(tzinfo=UTC)
    try:
        return local.replace(tzinfo=ZoneInfo(match.group(1)))
    except (ZoneInfoNotFoundError, ValueError):
        return local.replace(tzinfo=UTC)


def _walk(part: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not part:
        return []
    found = [part]
    for child in part.get("parts") or ():
        found.extend(_walk(child))
    return found
