"""What a rung promises the ladder, and nothing more.

Keeping this in its own module is what lets the ladder hold rungs it does not
import: rung 3 talks to a model over the network and rung 4 is a human, and
neither should be a compile-time dependency of the thing that orders them.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from loop.domain.messages import CandidateMessage, Comp, Intent
from loop.domain.types import Rung

from .registry import RuleRegistry


@dataclass(frozen=True, slots=True)
class LadderContext:
    """Everything a rung may know that is not in the message.

    Read once by the caller, before any transaction is opened. The TypeScript
    fetched the thread map from inside the transaction that also awaited the
    model, which is how a connection came to sit idle for the length of an
    inference.
    """

    registry: RuleRegistry
    # Threads already attached to an application, mapped to that application.
    thread_to_application: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Extraction:
    """One rung's reading of one message.

    A rung MUST abstain — return None — rather than guess, so that the next rung
    sees the message instead of inheriting a claim nobody stands behind.
    """

    intent: Intent
    confidence: float
    rung: Rung
    company: str | None = None
    role: str | None = None
    stage_hint: str | None = None
    deadline: str | None = None
    comp: Comp | None = None
    decide_by: datetime | None = None


class ExtractionRung(Protocol):
    @property
    def costly(self) -> bool:
        """True for a rung a `cheap_only` message may not pay for."""

    def extract(self, msg: CandidateMessage, ctx: LadderContext) -> Extraction | None: ...
