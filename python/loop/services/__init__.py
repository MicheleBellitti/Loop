"""The long-running processes: one consumer loop, one handler each.

Nothing in here decides anything. The decisions are in `loop.domain`,
`loop.ladder` and `loop.resolver`, all of which are pure; these are the shells
that fetch what a decision needs, run it, and write down what it said.

That split is what makes the shape in `consumer.py` possible — claim, work with
no connection held, then write — and it is the reason a model call in P4 can
take thirty seconds without holding anything open.
"""

from .consumer import Consumer, ConsumerOptions, Handler
from .pipeline import Applied, PipelineService
from .resolver import Resolved, ResolverService

__all__ = [
    "Applied",
    "Consumer",
    "ConsumerOptions",
    "Handler",
    "PipelineService",
    "Resolved",
    "ResolverService",
]
