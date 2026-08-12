"""Reading the two corpora the ladder is measured against.

**Fixtures** — `fixtures/*.eml` with expectations in `fixtures/manifest.json`.
Committed, so CI runs them, and synthetic, so they prove much less than they
appear to: they were written from the same reading of the spec that produced the
rules, which is how the TypeScript came to report perfect precision while
matching almost nothing in a real mailbox. A regression net, not evidence.

**Baseline** — `fixtures/private/ladder-baseline.jsonl`, one real message per
line with the verdict the TypeScript gave it. Produced by
`scripts/export-baseline.ts`, never committed, and the only corpus that says
anything about whether this works.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import Message
from hashlib import sha256
from pathlib import Path
from typing import Any

from loop.domain import normalise_message
from loop.domain.messages import CalendarInvite, InviteMethod, MessageHeaders, RawMessage

DEFAULT_RECEIVED_AT = datetime(2026, 7, 30, 9, 12, tzinfo=UTC)

# A decoded JSON object. `Any` is the honest type at this boundary: the file is
# written by another implementation and validating it into a model here would
# duplicate the shapes it is being compared against.
JsonObject = dict[str, Any]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class FixtureCase:
    message: RawMessage
    # What the fixture claims: intent, company, vendor, drop, requires_model.
    expect: Mapping[str, object]
    path: str


def load_fixtures(root: Path | None = None) -> list[FixtureCase]:
    base = root or repo_root()
    manifest = json.loads((base / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    return [
        FixtureCase(
            message=parse_eml((base / case["file"]).read_bytes(), case["file"]),
            expect=case["expect"],
            path=case["file"],
        )
        for case in manifest
    ]


def parse_eml(raw: str | bytes, name: str) -> RawMessage:
    """A real MIME parse, where the TypeScript hand-rolled one.

    `email` is in the standard library, handles multipart and encodings, and
    removes the fixture parser as a source of differences between the two
    implementations.

    From bytes, because a message declares its own charset: parsing a decoded
    string and asking for the payload back re-encodes it as ASCII, which turned
    "Senior Engineer è stata inviata" into a replacement character. The modern
    policy decodes RFC 2047 headers too, so a subject arrives as text rather
    than as `=?UTF-8?B?…?=`.
    """
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    parsed = message_from_bytes(data, policy=policy.default)
    text, html, invite = _walk_parts(parsed)
    body = normalise_message(html=html) if html and not text else normalise_message(text=text)

    return RawMessage(
        user_id="corpus",
        mailbox_id="corpus",
        provider_message_id=name,
        thread_id=None,
        received_at=_parse_date(parsed.get("date")),
        headers=MessageHeaders(
            message_id=parsed.get("message-id") or name,
            sender=parsed.get("from") or "",
            subject=parsed.get("subject") or "",
            date=parsed.get("date") or "",
            to=tuple(a.strip() for a in (parsed.get("to") or "").split(",") if a.strip()),
            list_id=parsed.get("list-id"),
            list_unsubscribe=parsed.get("list-unsubscribe"),
            precedence=parsed.get("precedence"),
        ),
        text=body.text,
        body_sha256=sha256(body.text.encode("utf-8")).hexdigest(),
        invite=invite,
    )


def _walk_parts(message: Message) -> tuple[str, str, CalendarInvite | None]:
    text = html = ""
    invite: CalendarInvite | None = None
    for part in message.walk():
        if part.is_multipart():
            continue
        content = _decode(part)
        kind = part.get_content_type()
        if kind == "text/plain" and not text:
            text = content
        elif kind == "text/html" and not html:
            html = content
        elif kind in {"text/calendar", "application/ics"} and invite is None:
            invite = parse_ics(content)
    return text, html, invite


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    raw = part.get_payload()
    return raw if isinstance(raw, str) else ""


def parse_ics(ics: str) -> CalendarInvite | None:
    """Enough of an .ics to place an interview.

    Read from inside the VEVENT: a VTIMEZONE block carries its own DTSTART, with
    a year in the sixteen-hundreds, and reading the first one in the file put
    interviews four centuries in the past.
    """
    unfolded = ics.replace("\r\n", "\n").replace("\n ", "").replace("\n\t", "")
    start_marker, end_marker = "BEGIN:VEVENT", "END:VEVENT"
    if start_marker in unfolded and end_marker in unfolded:
        vevent = unfolded.split(start_marker, 1)[1].split(end_marker, 1)[0]
    else:
        vevent = unfolded

    fields = _ics_fields(vevent)
    method = _INVITE_METHODS.get(_ics_fields(unfolded).get("METHOD", "").upper())
    uid, starts = fields.get("UID"), fields.get("DTSTART")
    if not uid or not starts:
        return None

    organiser = fields.get("ORGANIZER")
    return CalendarInvite(
        uid=uid,
        summary=fields.get("SUMMARY"),
        starts_at=_ics_datetime(starts),
        ends_at=_ics_datetime(fields["DTEND"]) if "DTEND" in fields else None,
        location=fields.get("LOCATION"),
        organiser=organiser.split(":")[-1] if organiser else None,
        status="cancelled" if fields.get("STATUS", "").upper() == "CANCELLED" else "confirmed",
        method=method,
    )


_INVITE_METHODS: dict[str, InviteMethod] = {
    "REQUEST": "REQUEST",
    "CANCEL": "CANCEL",
    "REPLY": "REPLY",
    "PUBLISH": "PUBLISH",
}


def _ics_fields(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in block.split("\n"):
        name, _, value = line.partition(":")
        key = name.split(";")[0].strip().upper()
        if key and value and key not in fields:
            fields[key] = value.strip()
    return fields


def _ics_datetime(value: str) -> datetime:
    cleaned = value.strip().rstrip("Z")
    for layout in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(cleaned, layout).replace(tzinfo=UTC)
        except ValueError:
            continue
    return _parse_date(value)


def _parse_date(value: str | None) -> datetime:
    if not value:
        return DEFAULT_RECEIVED_AT
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return DEFAULT_RECEIVED_AT
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class BaselineCase:
    """One real message and the verdict the TypeScript gave it."""

    message: RawMessage
    score: int
    outcome: str
    intent: str | None
    company: str | None
    role: str | None
    confidence: float | None
    rung: int | None
    vendor: str | None
    stage_hint: str | None


@dataclass(frozen=True, slots=True)
class BaselineContext:
    """What the TypeScript knew about this user when it judged these messages.

    Carried with the corpus because the classifier's score depends on it: a
    reply on an owned thread is worth two points, and diffing without the same
    thread map compares two different questions.
    """

    company_domains: frozenset[str] = frozenset()
    known_threads: frozenset[str] = frozenset()
    known_newsletters: frozenset[str] = frozenset()
    thread_to_application: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Baseline:
    context: BaselineContext
    cases: tuple[BaselineCase, ...]


def default_baseline_path() -> Path:
    return repo_root() / "fixtures" / "private" / "ladder-baseline.jsonl"


def load_baseline(path: Path | None = None) -> Baseline:
    source = path or default_baseline_path()
    if not source.exists():
        raise FileNotFoundError(
            f"{source} does not exist. Produce it with `npm run export:baseline`, which "
            "reads the mailbox once and writes the TypeScript's verdict for every message."
        )

    context = BaselineContext()
    cases: list[BaselineCase] = []
    for line in _lines(source):
        row = json.loads(line)
        if row.get("kind") == "context":
            context = _baseline_context(row)
        else:
            cases.append(_baseline_case(row))
    return Baseline(context, tuple(cases))


def _baseline_context(row: JsonObject) -> BaselineContext:
    return BaselineContext(
        company_domains=frozenset(row.get("company_domains") or ()),
        known_threads=frozenset(row.get("known_threads") or ()),
        known_newsletters=frozenset(row.get("known_newsletters") or ()),
        thread_to_application=dict(row.get("thread_to_application") or {}),
    )


def _lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield line


def _baseline_case(row: JsonObject) -> BaselineCase:
    verdict: JsonObject = row.get("verdict") or {}
    return BaselineCase(
        message=message_from_json(row["message"]),
        score=int(verdict.get("score", 0)),
        outcome=str(verdict.get("outcome", "drop")),
        intent=verdict.get("intent"),
        company=verdict.get("company"),
        role=verdict.get("role"),
        confidence=verdict.get("confidence"),
        rung=verdict.get("rung"),
        vendor=verdict.get("vendor"),
        stage_hint=verdict.get("stage_hint"),
    )


def message_from_json(row: JsonObject) -> RawMessage:
    """A `RawMessage` as the TypeScript serialises it onto the queue."""
    headers: JsonObject = row.get("headers") or {}
    invite: JsonObject | None = row.get("invite")
    return RawMessage(
        user_id=row.get("user_id", "baseline"),
        mailbox_id=row.get("mailbox_id", "baseline"),
        provider_message_id=row["provider_message_id"],
        thread_id=row.get("thread_id"),
        received_at=_parse_date(row.get("received_at")),
        headers=MessageHeaders(
            message_id=headers.get("message_id", ""),
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            date=headers.get("date", ""),
            to=tuple(headers.get("to") or ()),
            list_id=headers.get("list_id"),
            list_unsubscribe=headers.get("list_unsubscribe"),
            precedence=headers.get("precedence"),
            auto_submitted=headers.get("auto_submitted"),
        ),
        text=row.get("text", ""),
        body_sha256=row.get("body_sha256", ""),
        invite=_invite_from_json(invite) if invite else None,
    )


def _invite_from_json(row: JsonObject) -> CalendarInvite:
    return CalendarInvite(
        uid=str(row.get("uid", "")),
        summary=row.get("summary"),
        starts_at=_parse_date(row.get("starts_at")),
        ends_at=_parse_date(row["ends_at"]) if row.get("ends_at") else None,
        location=row.get("location"),
        organiser=row.get("organiser"),
        attendees=tuple(row.get("attendees") or ()),
        status=row.get("status") or "confirmed",
        method=row.get("method"),
    )
