"""The long-running processes: one consumer loop, one handler each.

Nothing in here decides anything. The decisions are in `loop.domain`,
`loop.ladder` and `loop.resolver`, all of which are pure; these are the shells
that fetch what a decision needs, run it, and write down what it said.

That split is what makes the shape in `consumer.py` possible — claim, work with
no connection held, then write — and it is the reason a model call in P4 can
take thirty seconds without holding anything open.
"""

from .classifier import ClassifierService, Screened
from .consumer import Consumer, ConsumerOptions, Handler
from .extractor import ExtractorService, Reading, TransientRungError
from .notifier import Delivered, NotifierService
from .nudge import NudgeService, Ticked, suggestion_payload
from .pipeline import Applied, PipelineService
from .push import PushPayload, PushResult, PushSender, PushSubscription, VapidConfig
from .resolver import Resolved, ResolverService

__all__ = [
    "Applied",
    "ClassifierService",
    "Consumer",
    "ConsumerOptions",
    "Delivered",
    "ExtractorService",
    "Handler",
    "NotifierService",
    "NudgeService",
    "PipelineService",
    "PushPayload",
    "PushResult",
    "PushSender",
    "PushSubscription",
    "Reading",
    "Resolved",
    "ResolverService",
    "Screened",
    "Ticked",
    "TransientRungError",
    "VapidConfig",
    "suggestion_payload",
]
