"""Where things are on disk, answered once.

Twelve call sites had worked this out for themselves — `parents[1]`,
`parents[2]`, `parents[3]`, under three different names, two of them honouring
an environment override and ten not. That is a magic index per directory the
code needs, and moving or nesting one of them means finding all twelve; the one
that is missed fails only on the code path that reads that directory, and
`loop.api.app` does not even fail, it serves 404s for the whole PWA.

Two roots, because there genuinely are two. `backend_root()` holds the package
and the data it ships with — `rules/`, `migrations/`, `fixtures/`.
`repo_root()` is one above it and holds `frontend/`, `infra/` and `.env`, which
is what the launcher and the container tooling need.
"""

import os
from functools import lru_cache
from pathlib import Path

__all__ = ["backend_root", "migrations_dir", "repo_root", "rules_dir"]


@lru_cache(maxsize=1)
def backend_root() -> Path:
    """`backend/` — the directory holding `loop/`, and its data beside it."""
    return Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """The repository, one above `backend/`."""
    return backend_root().parent


def rules_dir() -> Path:
    """`LOOP_RULES_DIR` if it is set, which is how the tests point elsewhere."""
    override = os.environ.get("LOOP_RULES_DIR")
    return Path(override) if override else backend_root() / "rules" / "ats"


def migrations_dir() -> Path:
    return backend_root() / "migrations"
