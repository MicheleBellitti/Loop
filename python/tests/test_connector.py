"""The connector, and the reading it does before anything else sees a message.

Two halves. `normalise` is pure and is where the hard reading lives — which part
of a MIME tree is the message, what an invitation actually says. The client is
tested against a stub Google over the real protocol, which is the whole reason
it is hand-rolled: the parts worth testing are the backoff and the error
taxonomy, and a generated SDK would put its own transport in the way.
"""

import base64
from typing import Any

import httpx
import pytest

from loop.connector.normalise import parse_ics, to_raw_message
from loop.google.client import (
    GoogleAuthError,
    GoogleClient,
    GoogleRateLimit,
    HistoryTooOld,
    SyncTokenExpired,
)

AN_INVITE = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
UID:abc123@google.com
DTSTART:20260827T163000Z
DTEND:20260827T173000Z
SUMMARY:Technical interview — Prima
LOCATION:Dinova\\, Via Francesco Zanardi\\, 51\\, 40131 Bologna BO
ORGANIZER:mailto:careers@prima.it
ATTENDEE:mailto:me@gmail.com
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""


class TestReadingAnInvitation:
    def test_the_escaping_comes_off(self) -> None:
        invite = parse_ics(AN_INVITE)
        assert invite is not None
        # RFC 5545 escapes a comma in every text value. The reference never
        # unescaped it, so the location of every interview in the database
        # currently reads `Dinova\\, Via Francesco Zanardi\\, 51`.
        assert invite.location == "Dinova, Via Francesco Zanardi, 51, 40131 Bologna BO"

    def test_it_reads_the_things_a_stage_detector_needs(self) -> None:
        invite = parse_ics(AN_INVITE)
        assert invite is not None
        assert invite.uid == "abc123@google.com"
        assert invite.starts_at.isoformat() == "2026-08-27T16:30:00+00:00"
        assert invite.organiser == "careers@prima.it"
        assert invite.attendees == ("me@gmail.com",)
        assert invite.method == "REQUEST"

    def test_a_cancellation_says_so(self) -> None:
        invite = parse_ics(AN_INVITE.replace("STATUS:CONFIRMED", "STATUS:CANCELLED"))
        assert invite is not None
        # As much a fact about the application as the invitation was.
        assert invite.status == "cancelled"

    def test_a_folded_line_is_one_line(self) -> None:
        folded = AN_INVITE.replace(
            "SUMMARY:Technical interview — Prima",
            "SUMMARY:Technical interview\n  — Prima",
        )
        invite = parse_ics(folded)
        assert invite is not None
        assert invite.summary == "Technical interview — Prima"

    def test_a_local_time_carries_its_zone(self) -> None:
        invite = parse_ics(
            AN_INVITE.replace(
                "DTSTART:20260827T163000Z", "DTSTART;TZID=Europe/Rome:20260827T163000"
            )
        )
        assert invite is not None
        assert invite.starts_at.isoformat() == "2026-08-27T16:30:00+02:00"

    def test_something_that_is_not_a_calendar_is_nothing(self) -> None:
        assert parse_ics("hello") is None
        # No start is no invitation: the time is the whole of what it is for.
        assert parse_ics("BEGIN:VEVENT\nUID:x\nEND:VEVENT") is None


class TestReadingAMessage:
    def test_it_prefers_the_text_a_person_would_read(self) -> None:
        raw = to_raw_message(
            _a_message(
                parts=[
                    _part("text/plain", "This message requires HTML."),
                    _part("text/html", "<p>Grazie per la tua candidatura in Prima.</p>"),
                ]
            ),
            user_id="u",
            mailbox_id="m",
        )
        # A `multipart/alternative` recruiting mail often carries a plain-text
        # half that exists only to say it needs HTML.
        assert "candidatura" in raw.text

    def test_the_headers_a_rule_may_match_on_all_survive(self) -> None:
        raw = to_raw_message(
            _a_message(
                headers={"Auto-Submitted": "auto-generated", "List-Id": "<jobs.x>"}
            ),
            user_id="u",
            mailbox_id="m",
        )
        assert raw.headers.auto_submitted == "auto-generated"
        assert raw.headers.list_id == "<jobs.x>"

    def test_gmails_own_timestamp_wins_over_the_senders(self) -> None:
        raw = to_raw_message(
            _a_message(
                internal_date="1787913000000",
                headers={"Date": "Tue, 1 Jan 1980 00:00:00 +0000"},
            ),
            user_id="u",
            mailbox_id="m",
        )
        # The `Date` header is whatever the sender's machine believed; on bulk
        # mail that is regularly hours out and occasionally years.
        assert raw.received_at.year == 2026

    def test_the_invitation_is_found_wherever_it_is(self) -> None:
        raw = to_raw_message(
            _a_message(
                parts=[
                    _part("text/plain", "See attached."),
                    _part("application/octet-stream", AN_INVITE, filename="invite.ics"),
                ]
            ),
            user_id="u",
            mailbox_id="m",
        )
        assert raw.invite is not None
        assert raw.invite.uid == "abc123@google.com"


class TestTalkingToGoogle:
    async def test_a_404_on_history_means_the_cursor_is_stale(self) -> None:
        client = _client(
            {"/gmail/v1/users/me/history": (404, {"error": {"message": "gone"}})}
        )
        with pytest.raises(HistoryTooOld):
            await client.history("token", "42")

    async def test_but_a_404_on_a_message_does_not(self) -> None:
        # The reference mapped every 404 to a stale cursor, so a message deleted
        # between the listing and the fetch relisted the whole mailbox.
        client = _client({"/gmail/v1/users/me/messages/m1": (404, {})})
        with pytest.raises(RuntimeError) as raised:
            await client.get_message("token", "m1")
        assert not isinstance(raised.value, HistoryTooOld)

    async def test_a_revoked_grant_asks_the_user_and_a_quota_does_not(self) -> None:
        revoked = _client({"/gmail/v1/users/me/profile": (401, {})})
        with pytest.raises(GoogleAuthError) as first:
            await revoked.profile("token")
        assert first.value.needs_reauth is True

        quota = _client(
            {
                "/gmail/v1/users/me/profile": (
                    403,
                    {
                        "error": {
                            "message": "Rate Limit Exceeded",
                            "errors": [{"reason": "userRateLimitExceeded"}],
                        }
                    },
                )
            }
        )
        # A quota is not fixed by signing in again, and the reference showed
        # every one of these as the product's only full-screen failure.
        with pytest.raises(GoogleRateLimit):
            await quota.profile("token")

    async def test_a_500_is_retried_and_then_given_up_on(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, json={})

        client = GoogleClient(
            client_id="id",
            client_secret="secret",
            api_base="https://api.test",
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(GoogleRateLimit):
            await client._with_backoff(
                lambda: client._client().get("https://api.test/x"), attempts=3
            )
        assert attempts == 3

    async def test_an_expired_calendar_token_is_its_own_failure(self) -> None:
        client = _client({"/calendar/v3/calendars/primary/events": (410, {})})
        # Nothing in the reference caught this, so the dead token stayed in the
        # row and every run threw for ever.
        with pytest.raises(SyncTokenExpired):
            await client.list_calendar_events("token", sync_token="old")

    async def test_the_access_token_is_asked_for_once(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

        client = GoogleClient(
            client_id="id",
            client_secret="secret",
            oauth_base="https://oauth.test",
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        for _ in range(5):
            assert await client.access_token("mailbox-1", "refresh") == "at"
        # The reference refreshed inside the per-message ingest: a 250-message
        # backfill page cost 250 token round-trips.
        assert calls == 1

    async def test_an_invitation_gmail_did_not_inline_is_fetched(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "attachments" in request.url.path:
                return httpx.Response(
                    200, json={"data": _b64(AN_INVITE), "size": len(AN_INVITE)}
                )
            return httpx.Response(404, json={})

        client = GoogleClient(
            client_id="id",
            client_secret="secret",
            api_base="https://api.test",
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        message = _a_message(
            parts=[
                {"mimeType": "text/calendar", "body": {"attachmentId": "att-1", "size": 9000}}
            ]
        )

        await client.hydrate_calendar_parts("token", message)

        # Without this every real `.ics` in the mailbox parsed as nothing, and
        # the cheapest certain stage detector in the design never fired once.
        raw = to_raw_message(message, user_id="u", mailbox_id="m")
        assert raw.invite is not None


def _client(responses: dict[str, tuple[int, dict[str, Any]]]) -> GoogleClient:
    def handler(request: httpx.Request) -> httpx.Response:
        for path, (status, body) in responses.items():
            if request.url.path == path:
                return httpx.Response(status, json=body)
        return httpx.Response(200, json={})

    return GoogleClient(
        client_id="id",
        client_secret="secret",
        api_base="https://api.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).rstrip(b"=").decode()


def _part(
    mime_type: str, body: str, filename: str | None = None
) -> dict[str, Any]:
    return {
        "mimeType": mime_type,
        "filename": filename or "",
        "body": {"data": _b64(body), "size": len(body)},
    }


def _a_message(
    *,
    parts: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    internal_date: str = "1785225600000",
) -> dict[str, Any]:
    every_header = {
        "Message-Id": "<m1@mail.gmail.com>",
        "From": "Prima <careers@prima.it>",
        "To": "me@gmail.com",
        "Subject": "La tua candidatura",
        **(headers or {}),
    }
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": internal_date,
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": k, "value": v} for k, v in every_header.items()],
            "parts": parts or [_part("text/plain", "Abbiamo ricevuto la tua candidatura.")],
        },
    }

