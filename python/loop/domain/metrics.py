"""Metric envelopes and the display gates.

"Each response carries its own numerator, denominator and exclusion count — the
UI is required to show the denominator, so the API is required to send it."
Shipping a ratio without its note is called out in the handoff as a bug, so the
note is part of the value, not part of the template.
"""

from __future__ import annotations

from dataclasses import dataclass

from .thresholds import (
    CHANNEL_MIN_APPLICATIONS,
    RATIOS_MIN_CLOSED,
    SEASONAL_MIN_QUARTERS,
    SMALL_SAMPLE_MAX,
    TIME_IN_STAGE_MIN_TRANSITIONS,
)


@dataclass(frozen=True, slots=True)
class Metric:
    # None whenever the gate is unmet — there is no honest number to show.
    value: float | None
    numerator: float
    denominator: float
    # Applications deliberately left out, e.g. too recent to have converted.
    excluded: int
    gate_met: bool
    # "11 of 68 · 5 too recent to count" or "unlocks at 8 closed applications".
    note: str
    # Between the two gates the figure ships, flagged.
    small_sample: bool


def ratio(
    *,
    numerator: int,
    denominator: int,
    closed: int,
    excluded: int = 0,
    exclusion_reason: str = "excluded",
) -> Metric:
    """A ratio and the sentence that makes it honest.

    Below the gate the value is None and the note names the threshold, which is
    what turns an empty chart into a progress bar rather than a disappointment.
    """
    gate_met = closed >= RATIOS_MIN_CLOSED
    small_sample = gate_met and closed <= SMALL_SAMPLE_MAX

    if not gate_met:
        return Metric(
            value=None,
            numerator=numerator,
            denominator=denominator,
            excluded=excluded,
            gate_met=False,
            small_sample=False,
            note=f"{closed} closed · unlocks at {RATIOS_MIN_CLOSED} closed applications",
        )

    parts = [f"{numerator} of {denominator}"]
    if excluded > 0:
        parts.append(f"{excluded} {exclusion_reason}")
    if small_sample:
        parts.append("small sample")

    return Metric(
        value=None if denominator == 0 else numerator / denominator,
        numerator=numerator,
        denominator=denominator,
        excluded=excluded,
        gate_met=True,
        small_sample=small_sample,
        note=" · ".join(parts),
    )


def dwell_metric(p50: float | None, transitions: int) -> Metric:
    """Median dwell per stage. Gated on observed transitions, not applications."""
    gate_met = transitions >= TIME_IN_STAGE_MIN_TRANSITIONS
    return Metric(
        value=p50 if gate_met else None,
        numerator=p50 or 0,
        denominator=transitions,
        excluded=0,
        gate_met=gate_met,
        small_sample=False,
        note=(
            f"{transitions} transitions"
            if gate_met
            else f"{transitions} of {TIME_IN_STAGE_MIN_TRANSITIONS} stage changes needed"
        ),
    )


def channel_gate(applications: int) -> tuple[bool, str]:
    """A channel row is only shown once it has enough first-touch applications."""
    gate_met = applications >= CHANNEL_MIN_APPLICATIONS
    return gate_met, "" if gate_met else f"{applications} of {CHANNEL_MIN_APPLICATIONS} needed"


def seasonal_gate(quarters: int) -> tuple[bool, str]:
    """Seasonal shape is withheld rather than shown noisy."""
    gate_met = quarters >= SEASONAL_MIN_QUARTERS
    if gate_met:
        return True, ""
    missing = SEASONAL_MIN_QUARTERS - quarters
    plural = "" if missing == 1 else "s"
    return False, (
        f"Seasonal averages need {missing} more quarter{plural} of history before they "
        "mean anything, so they are hidden rather than shown noisy."
    )


def format_percent(value: float | None) -> str:
    """ "16.2%" — one decimal, because two implies a precision we do not have."""
    if value is None:
        return "—"
    s = f"{value * 100:.1f}"
    return f"{s[:-2] if s.endswith('.0') else s}%"


def format_days(value: float | None) -> str:
    if value is None:
        return "—"
    rounded = round(value * 10) / 10
    return f"{rounded:g} day{'' if rounded == 1 else 's'}"
