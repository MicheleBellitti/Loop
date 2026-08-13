"""Entity resolution: which application a signal is about.

The component whose mistakes are the most visible, because a wrong merge
rewrites history silently and a missed one splits a single job in two. Every
decision in here is a pure function of a signal and the rows it is compared
against; the queries that produce those rows live in the shell around it.
"""

from .company import CompanyLookup, domain_label, plan_lookup
from .embed import (
    EMBEDDING_DIMS,
    Embedder,
    LexicalEmbedder,
    cosine,
    parse_vector,
    to_vector,
)
from .events import events_for_signal
from .matching import (
    Ambiguous,
    Attached,
    Candidate,
    Created,
    Decision,
    Merge,
    country_of,
    decide,
    find_duplicate,
    merge_is_forbidden,
)

__all__ = [
    "EMBEDDING_DIMS",
    "Ambiguous",
    "Attached",
    "Candidate",
    "CompanyLookup",
    "Created",
    "Decision",
    "Embedder",
    "LexicalEmbedder",
    "Merge",
    "cosine",
    "country_of",
    "decide",
    "domain_label",
    "events_for_signal",
    "find_duplicate",
    "merge_is_forbidden",
    "parse_vector",
    "plan_lookup",
    "to_vector",
]
