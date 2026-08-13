"""Google, by hand.

Not `google-api-python-client`, and for one reason worth keeping: every host is
an injectable value, so the tests stand up a stub OAuth and Gmail server and
exercise this client over the real protocol rather than over a mock of it. A
generated SDK would put its own transport between the code and the thing being
tested, and the parts of this that are hard — the backoff, the error taxonomy,
which failure means "ask the user again" — all live in the transport.

Read-only scopes, and nothing here can send. That is the product's central
promise and it is enforced by never asking for the permission.

Five departures from the reference, each of them a bug it has today.

**A 404 means history expired only on the history path.** The reference mapped
every 404 to `HistoryTooOld`, so a message deleted between the listing and the
fetch triggered a thirty-day relist of the whole mailbox.

**A 403 is not always a revoked grant.** `accessNotConfigured`,
`userRateLimitExceeded` and `dailyLimitExceeded` are all 403, and none of them
is fixed by signing in again — but the reference showed every one of them as
the product's only full-screen failure.

**The access token is cached.** The reference refreshed once per message
ingested: a 250-message page cost 250 token round-trips.

**The token endpoint gets the same backoff as everything else**, so a blip
there no longer aborts a whole sync.

**`stop_watch` does not parse an empty body.** Gmail answers 204.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from loop.domain.thresholds import (
    BACKFILL_BATCH,
    BACKOFF_ATTEMPTS,
    BACKOFF_MAX_SECONDS,
    BACKOFF_MIN_SECONDS,
)

_log = logging.getLogger("loop.google")

DEFAULT_API_BASE: Final = "https://www.googleapis.com"
DEFAULT_OAUTH_BASE: Final = "https://oauth2.googleapis.com"
DEFAULT_CONSENT_BASE: Final = "https://accounts.google.com/o/oauth2/v2/auth"

# Read, and only read. `gmail.readonly` is enough for `users.watch`, so there is
# no scope here that could send, modify or delete anything.
SCOPES: Final = (
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/calendar.readonly"
)

_HISTORY_PATH = "/gmail/v1/users/me/history"

# 403 reasons that mean "not now" rather than "sign in again". Everything else
# with a 403 is treated as a grant that is gone.
_QUOTA_REASONS = frozenset(
    {
        "accessNotConfigured",
        "userRateLimitExceeded",
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "quotaExceeded",
        "servingLimitExceeded",
    }
)

# Long enough to matter, short enough that a clock skew cannot serve an expired
# token. Google issues these for an hour.
_TOKEN_MARGIN_SECONDS: Final = 120

_WHITESPACE = re.compile(r"\s+")
_ERROR_BODY_CHARS: Final = 2000
_ERROR_REASON_CHARS: Final = 300


class GoogleAuthError(Exception):
    """The credential is the problem.

    `needs_reauth` is the trigger for the product's only full-screen failure, so
    it is set when the grant is genuinely gone and not when a quota is.
    """

    def __init__(self, message: str, *, needs_reauth: bool) -> None:
        super().__init__(message)
        self.needs_reauth = needs_reauth


class GoogleRateLimit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"Google returned {status}")
        self.status = status


class HistoryTooOld(Exception):
    """The cursor is older than Gmail's memory. Recoverable, by relisting."""


class SyncTokenExpired(Exception):
    """The calendar's incremental token is too old. Same, with a full window."""


@dataclass(frozen=True, slots=True)
class Tokens:
    access_token: str
    expires_in: int
    scope: str = ""
    token_type: str = "Bearer"
    # Absent on a refresh: Google issues one only on the first grant, which is
    # why `prompt=consent` matters and why an absent one must never overwrite a
    # stored one.
    refresh_token: str | None = None


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: float


@dataclass(slots=True)
class GoogleClient:
    client_id: str
    client_secret: str
    api_base: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_API_BASE") or DEFAULT_API_BASE
    )
    oauth_base: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_OAUTH_BASE") or DEFAULT_OAUTH_BASE
    )
    http: httpx.AsyncClient | None = None
    _tokens: dict[str, _CachedToken] = field(default_factory=dict, init=False)

    # ── OAuth ───────────────────────────────────────────────────────────────

    @staticmethod
    def authorisation_url(
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        state: str,
        login_hint: str | None = None,
        consent_base: str | None = None,
    ) -> str:
        """Where to send the browser.

        `prompt=consent` is load-bearing rather than polite: it forces Google to
        re-issue a refresh token on every authorisation, and without one the
        reconnect path writes an empty secret over a working mailbox.
        """
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if login_hint:
            params["login_hint"] = login_hint
        base = consent_base or os.environ.get("GOOGLE_CONSENT_BASE") or DEFAULT_CONSENT_BASE
        return f"{base}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, redirect_uri: str, code_verifier: str
    ) -> Tokens:
        return await self._token_request(
            {
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> Tokens:
        return await self._token_request(
            {
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            }
        )

    async def access_token(self, mailbox_id: str, refresh_token: str) -> str:
        """A live access token, refreshed only when the last one is spent.

        The reference called `refresh` inside the per-message ingest, so a
        250-message backfill page made 250 token requests. Google issues these
        for an hour; this asks again a couple of minutes before that.
        """
        cached = self._tokens.get(mailbox_id)
        now = time.monotonic()
        if cached is not None and cached.expires_at > now:
            return cached.value
        tokens = await self.refresh(refresh_token)
        self._tokens[mailbox_id] = _CachedToken(
            tokens.access_token, now + max(0, tokens.expires_in - _TOKEN_MARGIN_SECONDS)
        )
        return tokens.access_token

    def forget_token(self, mailbox_id: str) -> None:
        self._tokens.pop(mailbox_id, None)

    async def revoke(self, token: str) -> None:
        """Fire and forget. A failed revoke must not block disconnecting."""
        try:
            await self._client().post(
                f"{self.oauth_base}/revoke", data={"token": token}, timeout=10
            )
        except httpx.HTTPError:
            _log.info("revoke did not complete; local state is removed regardless")

    # ── Gmail ───────────────────────────────────────────────────────────────

    async def watch(self, token: str, topic: str) -> dict[str, Any]:
        """Register for push. `expiration` comes back as milliseconds, as text."""
        return await self._call(
            token,
            "/gmail/v1/users/me/watch",
            method="POST",
            json_body={"topicName": topic, "labelIds": ["INBOX"]},
        )

    async def stop_watch(self, token: str) -> None:
        # 204, with no body. The reference parsed it as JSON and would have
        # thrown the first time anything called it.
        await self._call(token, "/gmail/v1/users/me/stop", method="POST", expect_body=False)

    async def profile(self, token: str) -> dict[str, Any]:
        """The cheapest way to learn a history id without reading any mail."""
        return await self._call(token, "/gmail/v1/users/me/profile")

    async def history(
        self, token: str, start_history_id: str, page_token: str | None = None
    ) -> dict[str, Any]:
        params = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._call(token, f"{_HISTORY_PATH}?{urlencode(params)}")

    async def list_messages(
        self,
        token: str,
        query: str,
        page_token: str | None = None,
        max_results: int = BACKFILL_BATCH,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return await self._call(token, f"/gmail/v1/users/me/messages?{urlencode(params)}")

    async def get_message(self, token: str, message_id: str) -> dict[str, Any]:
        return await self._call(
            token, f"/gmail/v1/users/me/messages/{message_id}?format=full"
        )

    async def get_attachment(
        self, token: str, message_id: str, attachment_id: str
    ) -> dict[str, Any]:
        return await self._call(
            token,
            f"/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}",
        )

    async def hydrate_calendar_parts(
        self, token: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        """Fetch the invitation Gmail did not inline.

        `format=full` inlines small parts and returns anything past a few
        kilobytes as a bare attachment id with an empty body — and a real
        calendar invitation is one of those. Without this, every `.ics` in the
        mailbox parsed as nothing, and the cheapest and most certain stage
        detector in the whole design never fired once.

        The predicate below is the same one `normalise` uses to find the part.
        Change one and you must change the other, or this fills a part nothing
        reads.
        """
        for part in _walk(message.get("payload")):
            if not _is_calendar(part):
                continue
            body = part.get("body") or {}
            if body.get("data") or not body.get("attachmentId"):
                continue
            try:
                attachment = await self.get_attachment(
                    token, message["id"], body["attachmentId"]
                )
            except (GoogleAuthError, GoogleRateLimit, HistoryTooOld, httpx.HTTPError):
                # An attachment can be gone and the message is still worth
                # reading. Logged, unlike the reference, which swallowed this
                # silently and so reproduced the original symptom with nothing
                # to find.
                _log.warning("could not hydrate an invitation on %s", message.get("id"))
                continue
            body["data"] = attachment.get("data")
        return message

    # ── Calendar ────────────────────────────────────────────────────────────

    async def list_calendar_events(
        self,
        token: str,
        *,
        sync_token: str | None = None,
        time_min: str | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "singleEvents": "true",
            "maxResults": 250,
            # Cancellations matter as much as invitations — an interview that
            # was called off is a fact about the application. The reference left
            # this off, which made all of its cancellation handling dead code.
            "showDeleted": "true",
        }
        # Never both: Google refuses a time window alongside an incremental
        # token.
        if sync_token:
            params["syncToken"] = sync_token
        elif time_min:
            params["timeMin"] = time_min
        if page_token:
            params["pageToken"] = page_token
        query = urlencode(params)
        return await self._call(token, f"/calendar/v3/calendars/primary/events?{query}")

    # ── transport ───────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=30)
        return self.http

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def _token_request(self, form: dict[str, str]) -> Tokens:
        response = await self._with_backoff(
            lambda: self._client().post(f"{self.oauth_base}/token", data=form)
        )
        if not response.is_success:
            reason = _read_error(response)
            # `invalid_grant` is the only body that means the user has to sign
            # in again; everything else here is the server's problem, not
            # theirs. The reference interpolated the whole raw body into a
            # message that reaches `last_error` and the logs.
            revoked = response.status_code == 401 or "invalid_grant" in reason
            raise GoogleAuthError(
                f"token request failed: {response.status_code} {reason}",
                needs_reauth=revoked,
            )
        body = response.json()
        return Tokens(
            access_token=body["access_token"],
            expires_in=int(body.get("expires_in", 0)),
            scope=body.get("scope", ""),
            token_type=body.get("token_type", "Bearer"),
            refresh_token=body.get("refresh_token"),
        )

    async def _call(
        self,
        token: str,
        path: str,
        *,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        expect_body: bool = True,
    ) -> dict[str, Any]:
        response = await self._with_backoff(
            lambda: self._client().request(
                method,
                f"{self.api_base}{path}",
                json=json_body,
                headers={"authorization": f"Bearer {token}"},
            )
        )

        if response.is_success:
            return response.json() if expect_body else {}

        reason = _read_error(response)
        where = f"{response.status_code} for {_scrubbed(path)}"
        if reason:
            where = f"{where}: {reason}"

        if response.status_code == 404 and path.startswith(_HISTORY_PATH):
            # Only here. The reference raised this for every 404, so a message
            # deleted between the listing and the fetch relisted the mailbox.
            raise HistoryTooOld(where)
        if response.status_code == 410:
            raise SyncTokenExpired(where)
        if response.status_code == 401 or (
            response.status_code == 403 and not _is_quota(reason)
        ):
            raise GoogleAuthError(f"Google returned {where}", needs_reauth=True)
        if response.status_code == 403:
            raise GoogleRateLimit(response.status_code)
        raise RuntimeError(f"Google returned {where}")

    async def _with_backoff(
        self, run: Any, attempts: int = BACKOFF_ATTEMPTS
    ) -> httpx.Response:
        """Retry what is worth retrying, with full jitter.

        Full jitter rather than a fixed doubling because the alternative is
        every client in a fleet waking at the same instant — and the point of
        backing off is to stop arriving together.
        """
        delay = float(BACKOFF_MIN_SECONDS)
        for attempt in range(1, attempts + 1):
            response: httpx.Response = await run()
            if response.status_code != 429 and response.status_code < 500:
                return response
            if attempt >= attempts:
                raise GoogleRateLimit(response.status_code)
            await asyncio.sleep(random.random() * delay)
            delay = min(delay * 2, float(BACKOFF_MAX_SECONDS))
        raise GoogleRateLimit(503)


def _walk(part: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not part:
        return []
    found = [part]
    for child in part.get("parts") or ():
        found.extend(_walk(child))
    return found


def _is_calendar(part: dict[str, Any]) -> bool:
    """The same test `normalise` applies when it looks for the invitation."""
    filename = part.get("filename") or ""
    return part.get("mimeType") == "text/calendar" or filename.lower().endswith(".ics")


def _is_quota(reason: str) -> bool:
    return any(name in reason for name in _QUOTA_REASONS)


def _read_error(response: httpx.Response) -> str:
    """Google's own sentence, capped, with nothing of the request in it.

    "Gmail API has not been used in project N before or it is disabled" turns a
    guess into a five-second fix, so it is worth carrying. An error body can
    also echo request content, and this string reaches a log that has to stay
    safe to keep — hence the cap and the preference for the structured message
    over the raw body.
    """
    try:
        text = response.text[:_ERROR_BODY_CHARS]
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        return _WHITESPACE.sub(" ", text)[:_ERROR_REASON_CHARS]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, str):
        described = error
    elif isinstance(error, dict):
        parts = [
            str(error.get(key))
            for key in ("status", "message")
            if error.get(key)
        ]
        reasons = [
            str(detail.get("reason"))
            for detail in (error.get("errors") or ())
            if isinstance(detail, dict) and detail.get("reason")
        ]
        described = " — ".join([*parts, *reasons])
    else:
        described = text
    return _WHITESPACE.sub(" ", described)[:_ERROR_REASON_CHARS]


def _scrubbed(path: str) -> str:
    """A path without its query string.

    A history cursor is not a secret, but a `q=` on the message list carries
    whatever was searched for, and nothing of what is in a mailbox belongs in a
    log line.
    """
    return path.split("?", 1)[0]


def base64url_decode(data: str) -> bytes:
    """Gmail's body encoding: base64 in the URL alphabet, padding optional."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)
