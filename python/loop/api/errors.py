"""One error envelope, and the mapping every failure funnels through.

    {"error": {"code": "bad_id", "message": "that is not an application id",
               "field": "id"}}

The client reads `code` and nothing else. `message` is free text for a human
reading a log; `code` is the contract, so a new one is a change to the client's
behaviour whether or not the client changes.

`field` is present only when a handler names one. `"field": null` is a
contract violation rather than a harmless extra, which is why the envelope is
built here and not at each raise site.
"""

from typing import Any


class ApiError(Exception):
    """A failure with a code the client knows."""

    def __init__(self, status: int, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field

    def body(self) -> dict[str, Any]:
        return envelope(self.code, self.message, self.field)


def envelope(code: str, message: str, field: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        error["field"] = field
    return {"error": error}


def code_for(status: int) -> str:
    """What an uncoded failure becomes.

    A 500 also loses its message: the reference replaces it with a fixed string
    so a SQL error never reaches a browser. Anything else keeps its message,
    which is how a 409 or a rate limit still says something useful while
    carrying the generic code the client already handles.
    """
    if status == 404:
        return "not_found"
    if status >= 500:
        return "internal"
    return "bad_request"


INTERNAL_MESSAGE = "something failed"


def unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "sign in first")


def bad_csrf() -> ApiError:
    return ApiError(403, "csrf", "missing or invalid CSRF token")


def not_found(message: str = "no such endpoint") -> ApiError:
    return ApiError(404, "not_found", message)
