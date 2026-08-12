"""The stage machine.

Stages are per-user configurable; the phase a stage belongs to is what
statistics aggregate on. Depth is for display and for "how far did it get" —
never a gate on a transition.
"""

from __future__ import annotations

from collections.abc import Iterable

from .types import AppStatus, Phase, StageDef

# The default stage set, seeded per user and editable afterwards.
#
# `draft` from Engineering Spec §10 is absent: no event in the catalogue can
# produce it (quick add emits `applied`), so it was unreachable, and it was
# excluded from every metric anyway. Depth 0 is left free so a future `saved`
# event can reintroduce it without renumbering anything. decisions.md A7.
#
# Depths 3 and 11 are deliberately unused — the spec leaves them free so a user
# can insert a stage of their own between two defaults.
DEFAULT_STAGE_DEFS: tuple[StageDef, ...] = (
    StageDef("applied", "Applied", "sent", 1, 21),
    StageDef("acknowledged", "Acknowledged", "sent", 2, 21),
    StageDef("recruiter_reachout", "Recruiter reach-out", "screening", 4, 10),
    StageDef("hr_call", "HR call", "screening", 5, 10),
    StageDef("take_home", "Take-home", "screening", 6, 14),
    StageDef("technical", "Technical", "interviewing", 7, 12),
    StageDef("system_design", "System design", "interviewing", 8, 12),
    StageDef("onsite_loop", "Onsite loop", "interviewing", 9, 14),
    StageDef("final", "Final", "interviewing", 10, 10),
    StageDef("offer", "Offer", "decided", 12, 7),
    StageDef("negotiating", "Negotiating", "decided", 13, 7),
)

# Groups render in this fixed order; empty groups are omitted entirely.
PHASE_ORDER: tuple[Phase, ...] = ("interviewing", "screening", "sent", "decided")

PHASE_LABELS: dict[Phase, str] = {
    "sent": "Sent",
    "screening": "Screening",
    "interviewing": "Interviewing",
    "decided": "Decided",
}


class StageTable:
    """A user's stage set.

    Every lookup goes through a table the caller supplies, because
    `stage_defs` is editable. Renaming a stage is a label change, never a data
    migration — which is why existing events keep their original `to_stage`
    string and an unknown key degrades gracefully instead of raising.
    """

    __slots__ = ("_by_key",)

    def __init__(self, defs: Iterable[StageDef] | None = None) -> None:
        self._by_key: dict[str, StageDef] = {d.key: d for d in (defs or DEFAULT_STAGE_DEFS)}

    def get(self, key: str) -> StageDef | None:
        return self._by_key.get(key)

    def all(self) -> list[StageDef]:
        return sorted(self._by_key.values(), key=lambda d: d.depth)

    def phase_of(self, key: str) -> Phase:
        """Unknown stages land in `sent`: visible and uncounted rather than lost."""
        d = self._by_key.get(key)
        return d.phase if d else "sent"

    def depth_of(self, key: str) -> int:
        d = self._by_key.get(key)
        return d.depth if d else 0

    def label_of(self, key: str) -> str:
        d = self._by_key.get(key)
        return d.label if d else key

    def stale_after_days(self, key: str) -> int:
        d = self._by_key.get(key)
        return d.stale_after_days if d else 21

    def is_forward(self, from_stage: str | None, to_stage: str) -> bool:
        """Whether a transition moves deeper.

        Depth is for display and for "how far did it get" only. It MUST NOT be
        used to reject a transition — skipping forward is normal (referrals
        start deep) and moving back is legitimate (a round was added). This
        exists so the headline can count *forward* movement, not so anything
        can be blocked.
        """
        if not from_stage:
            return True
        return self.depth_of(to_stage) > self.depth_of(from_stage)


DEFAULT_STAGES = StageTable()


def display_stage(
    status: AppStatus,
    stage: str,
    stages: StageTable | None = None,
    *,
    presumed_closed: bool = False,
) -> str:
    """What the interface shows in the stage column.

    The prototypes draw `Rejected` and `Dormant` where a stage goes, while the
    schema calls them statuses. Status wins, and the mapping happens here, on
    the server, because the client is not allowed to derive a stage.
    decisions.md A6.
    """
    if status == "rejected":
        return "Rejected"
    if status == "withdrawn":
        return "Withdrawn"
    if status == "accepted":
        return "Accepted"
    if status == "dormant":
        # Dormant means "no reply yet". Past the long threshold it means "you
        # were passed over and nobody said so", and saying the weaker thing
        # leaves a pipeline full of processes that ended months ago.
        return "Closed by silence" if presumed_closed else "Dormant"
    return (stages or DEFAULT_STAGES).label_of(stage)


def is_closed(status: AppStatus) -> bool:
    """Closed applications render dimmed, never hidden."""
    return status != "live"
