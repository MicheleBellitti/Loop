"""Turning values into exactly the JSON the client already expects.

The success condition for this API is that the existing PWA, unmodified, points
at it and works. That makes the wire format the specification, and it makes
three JavaScript habits load-bearing.

**A key that is absent is not a key that is null.** `JSON.stringify` omits
`undefined` and keeps `null`, and the client distinguishes them: `error.field`
must vanish when there is none, while `next_interview` must be present and null.
Nothing here may use FastAPI's `response_model` or any `exclude_*` option —
those drop keys, reorder them and coerce types. Handlers return plain dicts.

**`1.0` serialises as `1`.** JavaScript has one number type, so an integral
float loses its decimal point on the wire. Python writes `1.0` and the diff
against the reference lights up on every confidence and every ratio.

**Some numbers are strings.** `bigint` comes back from node-postgres as a string
unless it is cast, so `events[].id` and `comp_offers.min_minor` are quoted on one
route and numeric on another. That is an accident of a driver rather than a
design, and it is still the contract: the client's `money()` swallows both, so
nothing here would catch a well-meaning correction.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def iso_z(moment: datetime | None) -> str | None:
    """`2026-08-13T09:41:07.482Z` — milliseconds, and a Z rather than +00:00.

    What `Date.prototype.toISOString` produces, which is what every timestamp on
    this API looks like today. The client only calls `new Date(...)` and would
    accept an offset, but a fixture diff against the reference would flag every
    single timestamp.
    """
    if moment is None:
        return None
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def num(value: float | Decimal | None) -> int | float | None:
    """A number the way JavaScript would have written it."""
    if value is None:
        return None
    as_float = float(value)
    return int(as_float) if as_float.is_integer() else as_float


def quoted(value: int | Decimal | str | None) -> str | None:
    """A `bigint` as node-postgres hands it over: a string.

    Used where the reference leaves the cast off — an event id, an offer's
    minor units on the detail route. `/api/stats` casts the same column and
    sends a number, and both are reproduced rather than reconciled.
    """
    return None if value is None else str(value)


def confidence(value: Decimal | float | None) -> Any:
    """Two decimals, as a string. `Number(c).toFixed(2)` in the reference."""
    return None if value is None else f"{float(value):.2f}"
