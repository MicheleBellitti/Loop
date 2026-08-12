"""The extraction ladder: classifier, then rungs 1 to 4.

Rung 1 is a template registry over `rules/ats/*.yaml`, rung 2 reads calendar
invites and the phrases recruiting mail is formulaic about, rung 3 is a local
model and rung 4 is the human. The rungs are tried in order and the first one
that does not abstain wins.
"""

from .classifier import Classification, ClassifierContext, classify
from .contracts import Extraction, ExtractionRung, LadderContext
from .ladder import (
    Extracted,
    Ignored,
    Ladder,
    LadderOutcome,
    NeedsReview,
    deterministic_ladder,
)
from .registry import RuleRegistry, RulesError
from .rung1 import TemplateRung
from .rung2 import HeuristicRung, stage_from_title
from .signal import build_signal, channel_for_vendor, stage_for_intent

__all__ = [
    "Classification",
    "ClassifierContext",
    "Extracted",
    "Extraction",
    "ExtractionRung",
    "HeuristicRung",
    "Ignored",
    "Ladder",
    "LadderContext",
    "LadderOutcome",
    "NeedsReview",
    "RuleRegistry",
    "RulesError",
    "TemplateRung",
    "build_signal",
    "channel_for_vendor",
    "classify",
    "deterministic_ladder",
    "stage_for_intent",
    "stage_from_title",
]
