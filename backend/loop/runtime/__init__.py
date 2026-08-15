"""The parts every process needs and none of them decides anything with.

One module so far: the log. It is here rather than in `loop.services` because
the API needs it too, and neither of those should import the other.
"""

from .log import ALLOWED_FIELDS, SECRET_KEY_PATTERN, configure_logging, redact

__all__ = ["ALLOWED_FIELDS", "SECRET_KEY_PATTERN", "configure_logging", "redact"]
