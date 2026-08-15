"""Deciding which application a signal belongs to.

"The hardest component, and the one whose mistakes are most visible: a wrong
merge silently rewrites history."

Every decision here is a pure function of a signal and the candidate rows. The
lookups that produce those rows are the shell's job, which is what lets the
thresholds — the numbers a wrong merge turns on — be tested without a database,
and what keeps the model call in P4 out of the transaction that holds them.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from loop.domain.messages import Signal
from loop.domain.thresholds import (
    AMBIGUITY_MARGIN,
    ATTACH_MULTI,
    ATTACH_SINGLE,
    DEDUP_MERGE,
    DEDUP_WINDOW_DAYS,
)

from .embed import cosine

# Stages where a merge would destroy something irreplaceable.
_UNMERGEABLE_STAGES = frozenset({"offer", "negotiating"})

_AMBIGUOUS_SHORTLIST = 3


@dataclass(frozen=True, slots=True)
class Candidate:
    """An open application at the same company, as the resolver compares them."""

    id: str
    embedding: list[float]
    applied_at: datetime | None = None
    status: str = "live"
    current_stage: str = "applied"
    work_mode: str | None = None
    location: str | None = None
    manually_created: bool = False
    # True when a human has already pulled this application apart from another.
    split_from: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Attached:
    application_id: str
    cosine: float


@dataclass(frozen=True, slots=True)
class Created:
    """Nothing here matches; the shell inserts a row."""


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """Two candidates too close to call. The system asks, once."""

    candidates: tuple[tuple[str, float], ...]


Decision = Attached | Created | Ambiguous


def decide(signal: Signal, embedding: list[float], candidates: list[Candidate]) -> Decision:
    """Which open application at this company the signal is about.

    Runs after thread identity has failed and the company is settled.
    """
    if not candidates:
        return Created()

    scored = sorted(
        ((c, cosine(embedding, c.embedding)) for c in candidates),
        key=lambda pair: pair[1],
        reverse=True,
    )
    best, best_cos = scored[0]

    if _names_no_role(signal):
        # Most real mail does not repeat the job title: a calendar invite says
        # "Interview with Prima", a rejection says "we will not be moving
        # forward". Embedding the placeholder gives a cosine near zero against
        # every candidate, so treating a roleless signal like any other created
        # a second application at the same company for every such message — one
        # employer became four rows, and the real application lost its own
        # rejection.
        #
        # With exactly one open application at the company, that application is
        # the only thing the message can be about. With more than one it stays
        # ambiguous and asks, as it should.
        if len(candidates) == 1:
            return Attached(best.id, 1.0)
        return _ambiguous(scored)

    if len(candidates) == 1:
        return Attached(best.id, best_cos) if best_cos >= ATTACH_SINGLE else Created()

    if best_cos < ATTACH_MULTI:
        return Created()
    if best_cos - scored[1][1] < AMBIGUITY_MARGIN:
        # Two candidates within a hair of each other. The system does not guess
        # here; it asks, once, and writes the answer back as a rule.
        return _ambiguous(scored)
    return Attached(best.id, best_cos)


def _names_no_role(signal: Signal) -> bool:
    return not signal.role or not (signal.role_normalised or "").strip()


def _ambiguous(scored: list[tuple[Candidate, float]]) -> Ambiguous:
    return Ambiguous(tuple((c.id, cos) for c, cos in scored[:_AMBIGUOUS_SHORTLIST]))


@dataclass(frozen=True, slots=True)
class Merge:
    """Two rows that are one application. The earlier one is kept."""

    keep: str
    merge: str
    cosine: float


MergeRefusal = Literal[
    "an offer or negotiation is open",
    "one of them was accepted",
    "one of them was declared by hand",
    "different work mode",
    "different country",
    "a human split them before",
]


def merge_is_forbidden(a: Candidate, b: Candidate) -> MergeRefusal | None:
    """Why these two must not be merged automatically, or None.

    A wrong merge rewrites history silently, so the exclusions are conservative
    and the reasons are named rather than counted — the user is shown why.
    """
    for app in (a, b):
        if app.current_stage in _UNMERGEABLE_STAGES:
            return "an offer or negotiation is open"
        if app.status == "accepted":
            return "one of them was accepted"
        if app.manually_created:
            return "one of them was declared by hand"
    if a.work_mode and b.work_mode and a.work_mode != b.work_mode:
        return "different work mode"
    if a.location and b.location and country_of(a.location) != country_of(b.location):
        return "different country"
    if b.id in a.split_from or a.id in b.split_from:
        return "a human split them before"
    return None


def find_duplicate(me: Candidate, others: list[Candidate]) -> Merge | None:
    """The same job, found twice.

    A job seen on LinkedIn and again on the company's own site is one
    application with two provenances, which is the thing that makes channel
    statistics honest in the first place.
    """
    if not me.embedding:
        return None
    for other in others:
        similarity = cosine(me.embedding, other.embedding)
        if similarity < DEDUP_MERGE:
            continue
        if _applied_too_far_apart(me, other) or merge_is_forbidden(me, other):
            continue
        # Keep the earliest as first touch: every timing statistic is measured
        # from it, and channel attribution depends on it. An application with no
        # applied_at yet cannot be the earlier of the two.
        if _is_earlier(me, other):
            return Merge(me.id, other.id, similarity)
        return Merge(other.id, me.id, similarity)
    return None


def _applied_too_far_apart(a: Candidate, b: Candidate) -> bool:
    if a.applied_at is None or b.applied_at is None:
        return False
    return abs((a.applied_at - b.applied_at).days) > DEDUP_WINDOW_DAYS


def _is_earlier(a: Candidate, b: Candidate) -> bool:
    if a.applied_at is None:
        return False
    if b.applied_at is None:
        return True
    return a.applied_at <= b.applied_at


# Crude, and deliberately so: it only has to separate Milan from Berlin.
_COUNTRIES: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset(
            {
                "milan",
                "milano",
                "rome",
                "roma",
                "turin",
                "torino",
                "napoli",
                "naples",
                "bologna",
                "italy",
                "italia",
                "trento",
                "biassono",
            }
        ),
        "it",
    ),
    (
        frozenset(
            {"berlin", "munich", "münchen", "hamburg", "frankfurt", "germany", "deutschland"}
        ),
        "de",
    ),
    (frozenset({"london", "manchester", "uk", "united kingdom"}), "gb"),
    (frozenset({"paris", "lyon", "france"}), "fr"),
    (frozenset({"madrid", "barcelona", "spain"}), "es"),
    (frozenset({"amsterdam", "netherlands"}), "nl"),
)


def country_of(location: str) -> str:
    lowered = location.lower()
    for places, code in _COUNTRIES:
        if any(place in lowered for place in places):
            return code
    return "unknown"
