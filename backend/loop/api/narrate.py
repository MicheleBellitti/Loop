"""Turning a log row into the two lines the drawer shows.

The event log is written for the fold, not for reading: a type, a stage key, a
jsonb payload whose keys depend on what produced it. What the drawer shows is a
sentence and a provenance note, and both are written here so that a second
client — the iOS app the whole design is pointed at — gets them for free rather
than reimplementing them.

`detail` is the interesting one, because its rules are a chain of truthiness
tests over an untyped payload and the order they are tried in is the whole
behaviour. It is reproduced exactly, including where that is unfortunate: a
`days_quiet` of zero is falsy and falls through to an empty string.
"""

from typing import Any

from loop.domain import StageTable

_TITLES = {
    "applied": "Applied",
    "acknowledged": "Acknowledged",
    "interview_scheduled": "Interview scheduled",
    "interview_held": "Interview held",
    "deadline_set": "Deadline detected",
    "offer_received": "Offer received",
    "offer_negotiated": "Offer revised",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
    "accepted": "Accepted",
    "went_silent": "Went quiet",
    "human_corrected": "You corrected this",
    "note_added": "Note",
}

# How much of a timestamp reads as a date and an hour: `2026-08-20T14:00`.
_TO_THE_MINUTE = 16

_HUMAN = 4


def title(event_type: str, to_stage: str | None, stages: StageTable) -> str:
    if event_type == "stage_advanced":
        return stages.label_of(to_stage) if to_stage else "Stage changed"
    # An unrecognised type shows its own name rather than nothing: a new event
    # type should look unfinished in the drawer, not invisible.
    return _TITLES.get(event_type, event_type)


def detail(payload: dict[str, Any]) -> str:
    """The first of these that has something in it wins."""
    note = payload.get("note")
    if isinstance(note, str):
        return note
    text = payload.get("text")
    if isinstance(text, str):
        return text
    if payload.get("ats_vendor"):
        return f"Automated reply from {payload['ats_vendor']}"
    if payload.get("starts_at"):
        return f"Invite for {_to_the_minute(payload['starts_at'])}"
    if payload.get("due_at"):
        return f"Due {_to_the_minute(payload['due_at'])}"
    if payload.get("field"):
        return f"{payload['field']}: {payload.get('from')} → {payload.get('to')}"
    if payload.get("days_quiet"):
        return f"No inbound signal for {payload['days_quiet']} days"
    return ""


def provenance(rung: int | None, payload: dict[str, Any]) -> str:
    """Where this came from, in three words.

    A null rung reads as `quick add` alongside rung 4, and that is right: both
    mean a human said so. Everything else names the mechanism, because "the
    calendar said so" and "the model thought so" are worth telling apart when
    you are deciding whether to believe a row.
    """
    if rung is None or rung == _HUMAN:
        return "quick add"
    if rung == 1:
        vendor = payload.get("ats_vendor")
        return f"gmail · {vendor}" if vendor else "gmail · template"
    if rung == 2:
        return "calendar · ics" if payload.get("calendar_event_id") else "gmail · thread"
    return "gmail · model"


def _to_the_minute(value: Any) -> str:
    """A slice, not a parse.

    The payload holds whatever was written into it, and the reference cuts the
    first sixteen characters and swaps the `T`. Parsing would be an improvement
    on days when the value is a timestamp and a crash on days when it is not.
    """
    return str(value)[:_TO_THE_MINUTE].replace("T", " ")
