"""The differential harness.

The TypeScript implementation is not the deliverable — it is the reference. It
found roughly twenty places where the spec was wrong and it proved on a real
twelve-month mailbox that mailbox-driven extraction works. This package is how
that knowledge is transferred without being taken on faith: both implementations
run over the same messages and the output is diffed, message by message, until
every difference is either a fixed bug or one of the deliberate improvements in
`divergences.py`.
"""

from .corpus import (
    STALE_FIXTURES,
    Baseline,
    BaselineCase,
    BaselineContext,
    FixtureCase,
    load_baseline,
    load_fixtures,
    parse_eml,
)
from .divergences import COMPARED_FIELDS, DIVERGENCES, Divergence, differing_fields, explain
from .runner import LadderRunner, Verdict, summarise

__all__ = [
    "COMPARED_FIELDS",
    "DIVERGENCES",
    "STALE_FIXTURES",
    "Baseline",
    "BaselineCase",
    "BaselineContext",
    "Divergence",
    "FixtureCase",
    "LadderRunner",
    "Verdict",
    "differing_fields",
    "explain",
    "load_baseline",
    "load_fixtures",
    "parse_eml",
    "summarise",
]
