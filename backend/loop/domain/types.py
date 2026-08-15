"""The vocabulary of the system.

Everything else in this package is a pure function over these shapes; nothing
here touches I/O.

Ported from `packages/domain/src/types.ts`. Where the two differ, the reason is
recorded in `docs/decisions.md` — that document, not the original Engineering
Spec, is the specification this package implements.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

# The unit every statistic aggregates on (Engineering Spec §01, §06).
Phase = Literal["sent", "screening", "interviewing", "decided"]

# A state, never a stage. `dormant` is reversible; the other three are not.
AppStatus = Literal["live", "dormant", "rejected", "withdrawn", "accepted"]

# How the application was first found. `referral` is never folded into a board.
Channel = Literal["linkedin", "indeed", "career_page", "referral", "recruiter", "other"]

# Which rung of the extraction ladder produced a claim. 4 is the human.
Rung = Literal[1, 2, 3, 4]

WorkMode = Literal["onsite", "hybrid", "remote"]

EventType = Literal[
    "applied",
    "acknowledged",
    "stage_advanced",
    "interview_scheduled",
    "interview_held",
    "deadline_set",
    "offer_received",
    "offer_negotiated",
    "rejected",
    "withdrawn",
    "accepted",
    "went_silent",
    "human_corrected",
    "note_added",
]

# The closed set of event types.
#
# Fourteen, not thirteen: the Engineering Spec §05 table and the Architecture
# sheet §05 list a *different* thirteen (`deadline_set` vs `note_added`), and
# the `prepare` nudge promises to surface the user's notes, so notes must
# exist. See decisions.md A2. Adding another one is a migration plus a change
# to the fold — never an ad-hoc string.
EVENT_TYPES: tuple[EventType, ...] = (
    "applied",
    "acknowledged",
    "stage_advanced",
    "interview_scheduled",
    "interview_held",
    "deadline_set",
    "offer_received",
    "offer_negotiated",
    "rejected",
    "withdrawn",
    "accepted",
    "went_silent",
    "human_corrected",
    "note_added",
)

# Statuses an automated rung may not move away from (Spec §10).
TERMINAL_STATUSES: frozenset[str] = frozenset({"rejected", "withdrawn", "accepted"})

CorrectableField = Literal[
    "stage",
    "status",
    "role_title",
    "seniority",
    "location",
    "work_mode",
    "company_id",
    "channel",
    "applied_at",
    "comp_expectation",
]

CORRECTABLE_FIELDS: tuple[CorrectableField, ...] = (
    "stage",
    "status",
    "role_title",
    "seniority",
    "location",
    "work_mode",
    "company_id",
    "channel",
    "applied_at",
    "comp_expectation",
)


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: str  # ISO-4217, uppercase


@dataclass(slots=True)
class DomainEvent:
    """One row of the append-only log, as the fold sees it.

    `payload` is deliberately a plain dict rather than a typed union: it mirrors
    a `jsonb` column, it is written by four different rungs, and forcing it into
    a closed model here would mean a schema migration every time a rule learns
    to extract one more field. The fold reads only the keys it understands and
    ignores the rest, which is also what makes an older queue message still
    resolve after a deploy.
    """

    type: EventType
    # When it happened in the world.
    occurred_at: datetime
    confidence: float
    # Stable identity used only for logging; never used to order the fold.
    id: str | int | None = None
    # When we learned it. Never affects the fold — only the UI's "picked up".
    recorded_at: datetime | None = None
    from_stage: str | None = None
    to_stage: str | None = None
    # Provider message/event id. Never a body. Doubles as the fold's tie-break.
    evidence_ref: str | None = None
    rung: Rung | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ApplicationState:
    """Everything the fold decides. One pure function of the event set."""

    current_stage: str
    current_phase: Phase
    status: AppStatus
    applied_at: datetime | None
    last_signal_at: datetime | None
    role_title: str | None
    seniority: str | None
    location: str | None
    work_mode: WorkMode | None
    company_id: str | None
    channel: Channel | None
    comp_expectation_minor: int | None
    comp_currency: str | None
    # Confidence of the event that decided the current stage.
    confidence: float


@dataclass(frozen=True, slots=True)
class StageDef:
    key: str
    label: str
    phase: Phase
    # Sort order and the "how far did it get" statistic. Never a gate.
    depth: int
    stale_after_days: int
