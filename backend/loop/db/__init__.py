"""Everything that talks to Postgres, and nothing that decides anything.

The split is deliberate and it is what phase 2 is for: `loop.domain` and
`loop.resolver` decide, this executes. A decision that needs a row asks for the
row to be passed in, which is why the fold, the thresholds and the merge
exclusions can all be tested in a tenth of a second with no database — and why
the model call in P4 can sit between two short transactions instead of inside
one.
"""

from .events import (
    append_event,
    apply_side_effects,
    load_events,
    load_stage_table,
    project_application,
    to_domain_event,
)
from .migrate import MigrationError, MigrationResult, default_migrations_dir, migrate
from .pool import Database
from .queue import (
    Message,
    Queue,
    acknowledge,
    claim,
    dead_letter,
    dead_letter_depth,
    depth,
    publish,
    publish_many,
)
from .rebuild import (
    rebuild_all,
    rebuild_application,
    refresh_projections,
    reset_projection,
    snapshot_applications,
)

__all__ = [
    "Database",
    "Message",
    "MigrationError",
    "MigrationResult",
    "Queue",
    "acknowledge",
    "append_event",
    "apply_side_effects",
    "claim",
    "dead_letter",
    "dead_letter_depth",
    "default_migrations_dir",
    "depth",
    "load_events",
    "load_stage_table",
    "migrate",
    "project_application",
    "publish",
    "publish_many",
    "rebuild_all",
    "rebuild_application",
    "refresh_projections",
    "reset_projection",
    "snapshot_applications",
    "to_domain_event",
]
