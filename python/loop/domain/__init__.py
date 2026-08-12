"""The domain: the fold, the stage machine, the thresholds, the rules.

Pure by construction — no database, no clock, no network. That is what lets its
tests run in a tenth of a second and what makes the port verifiable, because a
pure module can be diffed against the TypeScript one message at a time.

Re-derived from `docs/decisions.md`, not from the original Engineering Spec.
The spec was wrong in roughly twenty places; that document is the corrected
version and the reason each correction exists.
"""

from .clock import (
    at_local_time,
    days_between,
    hours_between,
    is_quiet_hour,
    next_deliverable_at,
    parse_quiet_hours,
    relative_future,
    relative_past,
)
from .denylist import fence_message, sanitise_model_output
from .flags import Flag, compute_flag, days_quiet, quiet_label
from .fold import fold, fold_with_provenance
from .headline import Headline, build_headline, date_eyebrow, number_word
from .metrics import Metric, channel_gate, dwell_metric, format_percent, ratio, seasonal_gate
from .normalise import (
    company_key,
    domain_of_address,
    matches_domain_suffix,
    normalise_company,
    normalise_role,
)
from .nudges import Suggestion, evaluate_nudges, rank_and_cap
from .stages import DEFAULT_STAGES, StageTable, display_stage, is_closed
from .types import ApplicationState, AppStatus, DomainEvent, EventType, Phase, StageDef

__all__ = [
    "DEFAULT_STAGES",
    "AppStatus",
    "ApplicationState",
    "DomainEvent",
    "EventType",
    "Flag",
    "Headline",
    "Metric",
    "Phase",
    "StageDef",
    "StageTable",
    "Suggestion",
    "at_local_time",
    "build_headline",
    "channel_gate",
    "company_key",
    "compute_flag",
    "date_eyebrow",
    "days_between",
    "days_quiet",
    "display_stage",
    "domain_of_address",
    "dwell_metric",
    "evaluate_nudges",
    "fence_message",
    "fold",
    "fold_with_provenance",
    "format_percent",
    "hours_between",
    "is_closed",
    "is_quiet_hour",
    "matches_domain_suffix",
    "next_deliverable_at",
    "normalise_company",
    "normalise_role",
    "number_word",
    "parse_quiet_hours",
    "quiet_label",
    "rank_and_cap",
    "ratio",
    "relative_future",
    "relative_past",
    "sanitise_model_output",
    "seasonal_gate",
]
