"""Running the deterministic ladder over a corpus and recording what it said.

One verdict per message, in the shape the TypeScript exporter writes, so the two
can be compared field by field rather than eyeballed.
"""

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

from loop.domain import domain_of_address
from loop.domain.messages import CandidateMessage, RawMessage
from loop.ladder import (
    ClassifierContext,
    Extracted,
    Ladder,
    LadderContext,
    RuleRegistry,
    classify,
    deterministic_ladder,
)


@dataclass(frozen=True, slots=True)
class Verdict:
    provider_message_id: str
    score: int
    outcome: str
    intent: str | None = None
    company: str | None = None
    role: str | None = None
    confidence: float | None = None
    rung: int | None = None
    vendor: str | None = None
    stage_hint: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class LadderRunner:
    """The classifier and the ladder, wired the way the extractor wires them."""

    def __init__(
        self,
        registry: RuleRegistry | None = None,
        *,
        classifier: ClassifierContext | None = None,
        ladder: Ladder | None = None,
        thread_to_application: dict[str, str] | None = None,
    ) -> None:
        self._registry = registry or RuleRegistry.load()
        self._ladder = ladder or deterministic_ladder()
        base = classifier or ClassifierContext()
        self._classifier = ClassifierContext(
            ats_domains=self._registry.ats_domains,
            company_domains=base.company_domains,
            known_threads=base.known_threads,
            known_newsletters=base.known_newsletters,
        )
        self._ctx = LadderContext(
            registry=self._registry, thread_to_application=thread_to_application or {}
        )

    def judge(self, msg: RawMessage) -> Verdict:
        classification = classify(msg, self._classifier)
        vendor = self._registry.vendor_for(domain_of_address(msg.headers.sender))

        if classification.outcome == "drop":
            return Verdict(
                provider_message_id=msg.provider_message_id,
                score=classification.score,
                outcome=classification.outcome,
                vendor=vendor,
            )

        candidate = CandidateMessage(
            message=msg,
            score=classification.score,
            cheap_only=classification.outcome == "cheap_only",
            reasons=classification.reasons,
        )
        outcome = self._ladder.run(candidate, self._ctx)
        if not isinstance(outcome, Extracted):
            return Verdict(
                provider_message_id=msg.provider_message_id,
                score=classification.score,
                outcome=classification.outcome,
                vendor=vendor,
            )

        signal = outcome.signal
        return Verdict(
            provider_message_id=msg.provider_message_id,
            score=classification.score,
            outcome=classification.outcome,
            intent=signal.intent,
            company=signal.company,
            role=signal.role,
            confidence=signal.confidence,
            rung=signal.rung,
            vendor=signal.ats_vendor,
            stage_hint=signal.stage_hint,
        )

    def judge_all(self, messages: Iterable[RawMessage]) -> list[Verdict]:
        return [self.judge(m) for m in messages]


def summarise(verdicts: Sequence[Verdict]) -> dict[str, int]:
    """Counts worth printing: how much was read, and by which rung."""
    counts = {
        "messages": len(verdicts),
        "dropped": sum(1 for v in verdicts if v.outcome == "drop"),
        "extracted": sum(1 for v in verdicts if v.intent is not None),
        "review": sum(1 for v in verdicts if v.outcome != "drop" and v.intent is None),
        "with_company": sum(1 for v in verdicts if v.company),
        "with_role": sum(1 for v in verdicts if v.role),
    }
    for rung in (1, 2):
        counts[f"rung_{rung}"] = sum(1 for v in verdicts if v.rung == rung)
    return counts
