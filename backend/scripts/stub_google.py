"""The stub Google server §17 asks for.

"A stub OAuth/Gmail server that replays fixture messages through the real
connector path." It speaks the seven endpoints the connector uses, serving the
golden corpus as if it were a mailbox — so the connector, classifier, extractor,
resolver and pipeline all run their real code against it, and the only thing
faked is Google.

This is the reason `loop.google.client` is hand-rolled over httpx with a
configurable base URL: pointing `GOOGLE_API_BASE` and `GOOGLE_OAUTH_BASE` here
is the whole setup, and no library needs mocking.

    uv run --extra ladder python scripts/stub_google.py
"""

import argparse
import base64
import calendar
import json
import re
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urlparse

PORT = 8787
FIXTURE_DIRS = ("fixtures/ats", "fixtures/negatives")
# UTC, via `calendar.timegm`. `time.mktime` reads the struct as *local* time
# and guesses DST, so the constant named `_FIXED_INSTANT` moved with the
# developer's timezone: 09:12Z in CI, 07:12Z in Europe/Rome, and the previous
# calendar day — inside the quiet-hours window e2e sets — in Pacific/Auckland.
# The fixtures' own `Date:` headers are timezone-qualified, so the harness and
# the stub have to agree on one instant or they measure the same message twice.
_FIXED_INSTANT = time.strptime("2026-07-30 07:12:00", "%Y-%m-%d %H:%M:%S")
INTERNAL_DATE = str(calendar.timegm(_FIXED_INSTANT) * 1000)


def backend_root() -> Path:
    """backend/, where `fixtures/` lives."""
    return Path(__file__).resolve().parents[1]


def load_fixtures(root: Path) -> dict[str, bytes]:
    messages: dict[str, bytes] = {}
    for directory in FIXTURE_DIRS:
        for path in sorted((root / directory).glob("*.eml")):
            messages[path.stem] = path.read_bytes()
    return messages


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def to_gmail_message(message_id: str, raw: bytes) -> dict[str, Any]:
    """`.eml` → the payload shape `messages.get(format=full)` returns.

    A real MIME parse, like `loop.harness.corpus.parse_eml` — hand-splitting on
    a blank line is how the reference's stub came to hand the connector a
    slightly different message from the one the harness measured.
    """
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    headers = [{"name": key, "value": str(value)} for key, value in parsed.items()]

    if parsed.is_multipart():
        parts = [
            {
                "mimeType": part.get_content_type(),
                "body": {"data": _b64url(part.get_content())},
                "headers": [],
            }
            for part in parsed.walk()
            if not part.is_multipart()
        ]
        payload: dict[str, Any] = {
            "mimeType": "multipart/mixed",
            "headers": headers,
            "parts": parts,
        }
    else:
        payload = {
            "mimeType": parsed.get_content_type(),
            "headers": headers,
            "body": {"data": _b64url(parsed.get_content())},
        }

    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "internalDate": INTERNAL_DATE,
        "payload": payload,
    }


class Handler(BaseHTTPRequestHandler):
    messages: ClassVar[dict[str, bytes]] = {}

    # `do_GET`/`do_POST` are BaseHTTPRequestHandler's spelling, not a choice.
    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def _route(self) -> None:
        path = urlparse(self.path).path

        # OAuth: any code exchanges, any refresh succeeds. The stub is not
        # testing Google's authentication, it is testing what happens after it.
        if path in ("/token", "/oauth2/v4/token"):
            return self._json(
                200,
                {
                    "access_token": "stub-access-token",
                    "refresh_token": "stub-refresh-token",
                    "expires_in": 3600,
                    "scope": (
                        "https://www.googleapis.com/auth/gmail.readonly"
                        " https://www.googleapis.com/auth/calendar.readonly"
                    ),
                    "token_type": "Bearer",
                },
            )

        if path == "/gmail/v1/users/me/profile":
            return self._json(200, {"emailAddress": "you@example.com", "historyId": "1000"})

        if path == "/gmail/v1/users/me/messages":
            listed = [{"id": i, "threadId": f"t-{i}"} for i in self.messages]
            return self._json(200, {"messages": listed, "resultSizeEstimate": len(listed)})

        detail = re.match(r"^/gmail/v1/users/me/messages/(.+)$", path)
        if detail:
            message_id = unquote(detail.group(1))
            raw = self.messages.get(message_id)
            if raw is None:
                return self._json(404, {"error": {"code": 404, "message": "not found"}})
            return self._json(200, to_gmail_message(message_id, raw))

        if path == "/gmail/v1/users/me/history":
            # Nothing new since the backfill: live sync is a no-op in the stub.
            return self._json(200, {"historyId": "1000"})

        if path == "/gmail/v1/users/me/watch":
            expires = int((time.time() + 7 * 86_400) * 1000)
            return self._json(200, {"historyId": "1000", "expiration": str(expires)})

        if path == "/gmail/v1/users/me/stop":
            return self._json(200, {})

        if path == "/calendar/v3/calendars/primary/events":
            return self._json(200, {"items": [], "nextSyncToken": "stub-sync-token"})

        self._json(404, {"error": {"code": 404, "message": f"stub has no route for {path}"}})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Quiet by default: the e2e run's own output is the thing to read."""


def serve(port: int, root: Path) -> ThreadingHTTPServer:
    Handler.messages = load_fixtures(root)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    server = serve(args.port, backend_root())
    print(
        f"stub google listening on http://localhost:{args.port}"
        f" with {len(Handler.messages)} messages"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
