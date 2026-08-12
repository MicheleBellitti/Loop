"""The ladder: rungs in order, first one that does not abstain wins.

"A rung MUST abstain rather than guess." A low-confidence reading is not a
better answer than no answer — it goes to a human, once, with the evidence
attached.

The whole ladder is a pure function of a message and a context. That is what
fixes the defect the TypeScript shipped with: there, the model call happened
inside the transaction that had claimed the queue item, so a connection sat
idle-in-transaction for the length of an inference and Postgres eventually
terminated it mid-flight. Here the caller reads what it needs, closes its
transaction, runs the ladder, and opens a second short transaction to append the
event — which the idempotency key on the event makes safe to retry.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from loop.domain import excerpt
from loop.domain.messages import CandidateMessage, Intent, Signal
from loop.domain.thresholds import REVIEW_BELOW

from .contracts import Extraction, ExtractionRung, LadderContext
from .rung1 import TemplateRung
from .rung2 import HeuristicRung
from .signal import build_signal


@dataclass(frozen=True, slots=True)
class Extracted:
    signal: Signal


@dataclass(frozen=True, slots=True)
class NeedsReview:
    """Rung 4 — ask the human, once."""

    excerpt: str
    # What the ladder managed to read, if anything. Shown as a starting point
    # rather than as an answer.
    intent: Intent | None
    confidence: float


LadderOutcome = Extracted | NeedsReview


class Ladder:
    def __init__(self, rungs: Sequence[ExtractionRung]) -> None:
        self._rungs = tuple(rungs)

    def run(self, msg: CandidateMessage, ctx: LadderContext) -> LadderOutcome:
        reading = self._read(msg, ctx)
        if reading is None or reading.confidence < REVIEW_BELOW:
            return NeedsReview(
                excerpt=excerpt(f'"{msg.text}" — {msg.headers.sender}'),
                intent=reading.intent if reading else None,
                confidence=reading.confidence if reading else 0.0,
            )
        return Extracted(build_signal(msg, reading, ctx))

    def _read(self, msg: CandidateMessage, ctx: LadderContext) -> Extraction | None:
        for rung in self._rungs:
            if rung.costly and msg.cheap_only:
                continue
            reading = rung.extract(msg, ctx)
            if reading is not None:
                return reading
        return None


def deterministic_ladder() -> Ladder:
    """Rungs 1 and 2 only — the ladder that needs no model and no network.

    This is the default posture with the model off, and it is what the
    differential harness measures.
    """
    return Ladder([TemplateRung(), HeuristicRung()])
