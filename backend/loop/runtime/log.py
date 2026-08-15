"""The log, and the two rules it enforces rather than documents.

"…never subject lines, never sender addresses, never body fragments. That single
line is enough to debug extraction, which is the point of keeping it clean
enough to be safe to keep." (Spec §16)

**Nothing that looks like a secret reaches a line.** `redact` walks any
structured value on its way out and replaces the value of a key matching
`token`, `password`, `secret`, `authorization`, `cookie`, `credential`,
`refresh` or `access_key`. That is the second half of "plaintext secrets exist
only inside the connector process, only for the length of one call, and are
never placed in a variable that a logger can reach" — the first half is a
convention, and this is the part that holds when someone logs a whole mailbox
row to find out why a sync failed.

**A structured field not on the allow-list is dropped.** `ALLOWED_FIELDS` is the
enforcement of the sentence above: adding `subject` to a debug line is a change
someone has to make deliberately, in this file, in a diff. It applies to the
structured half of a record — what `extra={…}` carries — because that is the
half that gets built out of a row and is where a body fragment would arrive
without anyone typing it.

The message half stays printf-style, as the rest of this codebase writes it. A
formatter cannot allow-list prose, and rewriting several hundred call sites into
field dictionaries to make it able to would be a large change to say something
the call sites already say.
"""

import dataclasses
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any, Final

SECRET_KEY_PATTERN: Final = re.compile(
    r"token|password|secret|authorization|cookie|credential|refresh|access_key",
    re.IGNORECASE,
)

REDACTED: Final = "[redacted]"

# The names `logging` accepts. Anything else is a configuration mistake, not a
# reason for a process to refuse to start.
_LEVEL_NAMES: Final[frozenset[str]] = frozenset(
    {"CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "NOTSET"}
)

ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "service",
        "level",
        "time",
        "msg",
        "mailbox_id",
        "provider_message_id",
        "user_id",
        "application_id",
        "company_id",
        "thread_id",
        "queue",
        "msg_id",
        "rung",
        "outcome",
        "confidence",
        "score",
        "duration_ms",
        "read_ct",
        "attempt",
        "count",
        "depth",
        "error",
        "code",
        "intent",
        "vendor",
        "decision",
        "rule",
        "status",
        "reason",
        "cosine",
        "threshold",
        "candidates",
        "stage",
        "phase",
        "event_type",
        "violations",
        "backfill",
        "history_id",
        "sync_token",
        "batch",
        "endpoint",
        "method",
    }
)

# Everything `logging` puts on a record itself. Anything else came from
# `extra={…}` and is therefore a field this module has an opinion about.
_RECORD_OWN_KEYS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


# A `key: value` or `key=value` pair in free text, where the key looks like a
# secret. Tracebacks and third-party exception messages are prose, so the only
# thing that can be done to them is this.
_PAIR_IN_TEXT: Final = re.compile(
    r"""(?ix)
    (?P<key>[A-Za-z_][A-Za-z0-9_\-]*
        (?:token|password|passwd|secret|authorization|cookie|credential|refresh|access_key)
        [A-Za-z0-9_\-]*)
    (?P<sep>\s*["']?\s*[:=]\s*["']?)
    (?P<value>[^\s,;'"})\]]+)
    """
)

# The password in a DSN: `postgres://loop:hunter2@host/db`. asyncpg puts the
# whole URL into some of its errors, and a `DATABASE_URL` in a traceback is a
# password in a log.
_DSN_PASSWORD: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)")

# `Bearer ya29…`, and the same for the other schemes an Authorization header
# carries, wherever it appears in prose.
_BEARER: Final = re.compile(r"(?i)\b(bearer|basic|token)\s+([A-Za-z0-9._~+/=\-]{8,})")

# Google's refresh and authorization codes are recognisable on their own, and
# they arrive in URLs and in prose where no key names them.
_GOOGLE_SECRET: Final = re.compile(r"\b(1//|ya29\.|4/0)[A-Za-z0-9._~+/=\-]{4,}")


def scrub_text(text: str) -> str:
    """The same rule as `redact`, applied to prose.

    `redact` can only act on a key it can see. A traceback has no keys — it is
    a string, and the secret inside it was put there by whoever raised. This is
    the fallback for every path where the structure is already gone: exception
    text, and any object whose repr is all that is left of it.
    """
    text = _DSN_PASSWORD.sub(rf"\1{REDACTED}\3", text)
    text = _PAIR_IN_TEXT.sub(rf"\g<key>\g<sep>{REDACTED}", text)
    text = _BEARER.sub(rf"\1 {REDACTED}", text)
    return _GOOGLE_SECRET.sub(REDACTED, text)


def _mapping_items(value: Any) -> Any:
    """`value.items()` if this behaves like a row, otherwise None.

    Duck-typed on purpose. `asyncpg.Record` is the object a mailbox row
    actually is, and it is neither a `dict` nor a `Mapping` nor a `Sequence` —
    it just has `keys()` and `items()`. Testing for the methods catches it, and
    catches the next row type without this file having to import it.
    """
    if isinstance(value, dict):
        return value.items()
    items = getattr(value, "items", None)
    keys = getattr(value, "keys", None)
    if callable(items) and callable(keys):
        try:
            return items()
        except Exception:  # pragma: no cover - a hostile object is not worth a crash
            return None
    return None


def redact(value: Any) -> Any:
    """Recursively replace anything that looks like a secret.

    Never raises and never drops a key: the shape of the object survives, so a
    log line still tells you which fields were present. A `bytes` becomes a
    marker rather than an encoding, because the only bytes this system holds are
    a ciphertext or a key.

    Three kinds of value reach this that are not dictionaries and have to be
    walked as if they were, because they are what a row and a token bundle are
    in this codebase: an `asyncpg.Record`, a dataclass (`Mailbox`, `Tokens`),
    and anything else whose repr is the only thing left — which is scrubbed as
    prose rather than trusted.
    """
    if isinstance(value, bytes | bytearray | memoryview):
        return "[bytes]"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    items = _mapping_items(value)
    if items is not None:
        return {
            key: REDACTED if SECRET_KEY_PATTERN.search(str(key)) else redact(item)
            for key, item in items
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: REDACTED
            if SECRET_KEY_PATTERN.search(f.name)
            else redact(getattr(value, f.name, None))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(item) for item in value]
    return scrub_text(str(value))


class SafeFormatter(logging.Formatter):
    """Redacts every argument and every structured field on the way out.

    Both halves matter. `log.info("synced %s", row)` interpolates the argument
    into the message, so the argument is redacted before the message is built;
    `log.info("synced", extra={"mailbox": row})` never touches the message, so
    the field is redacted and then checked against the allow-list.
    """

    def format(self, record: logging.LogRecord) -> str:
        return self._render(record, self._safe_message(record), _fields(record))

    def _safe_message(self, record: logging.LogRecord) -> str:
        if not record.args:
            return record.getMessage()
        args = record.args
        safe = redact(dict(args)) if isinstance(args, dict) else tuple(redact(a) for a in args)
        return str(record.msg) % safe

    def _render(self, record: logging.LogRecord, message: str, fields: dict[str, Any]) -> str:
        line = f"{self.formatTime(record)} {record.name} {record.levelname} {message}"
        if fields:
            line += " " + " ".join(f"{k}={v}" for k, v in sorted(fields.items()))
        if record.exc_info:
            line += "\n" + scrub_text(self.formatException(record.exc_info))
        return line


class JsonFormatter(SafeFormatter):
    """One JSON object per line, for a deployment that ships them somewhere."""

    def _render(self, record: logging.LogRecord, message: str, fields: dict[str, Any]) -> str:
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "service": record.name,
            "msg": message,
            **fields,
        }
        if record.exc_info:
            # `exception`, not `error`: `error` is an allow-listed field a
            # caller can set, and a traceback assigned after `**fields` would
            # silently overwrite whatever they meant by it.
            line["exception"] = scrub_text(self.formatException(record.exc_info))
        return json.dumps(line, default=str)


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    """The `extra={…}` half, redacted and then allow-listed."""
    return {
        key: redact(value)
        for key, value in record.__dict__.items()
        if key not in _RECORD_OWN_KEYS and key in ALLOWED_FIELDS
    }


def configure_logging(level: str | None = None, *, stream: Any | None = None) -> None:
    """Called once, at the top of every process.

    `LOG_FORMAT=json` switches the line format; `LOG_LEVEL` sets the level. Both
    have working defaults, because a process that will not start because its
    logging is misconfigured has failed at the one job that would have told you
    why.
    """
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        JsonFormatter() if os.environ.get("LOG_FORMAT") == "json" else SafeFormatter()
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]

    # `setLevel` raises on a name it does not know, and this runs before
    # anything else in every process — so `LOG_LEVEL=trace`, or an `info ` with
    # the trailing space a `.env` file carries, would take all eight containers
    # down with a ValueError and no line saying why. Fall back and say so.
    wanted = (level or os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    if wanted not in _LEVEL_NAMES:
        root.setLevel(logging.INFO)
        logging.getLogger("loop.runtime").warning(
            "unknown log level %r, using INFO", wanted, extra={"reason": "bad_log_level"}
        )
        return
    root.setLevel(wanted)
