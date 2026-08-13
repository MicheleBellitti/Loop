"""The queue's shapes, which are not quite the dataclasses'.

Pure: no database, no network. These are the tests that catch a payload the
scheduler writes and this cannot read — a class of bug that otherwise shows up
as "dormancy stopped working" three weeks later, with five dead letters and no
error anywhere.
"""

from datetime import UTC, datetime

import pytest

from loop.domain.messages import (
    CalendarInvite,
    CandidateMessage,
    Comp,
    EventSource,
    MessageHeaders,
    PendingEvent,
    RawMessage,
    Signal,
)
from loop.domain.wire import (
    decode_candidate_message,
    decode_pending_event,
    decode_raw_message,
    decode_signal,
    encode_candidate_message,
    encode_pending_event,
    encode_raw_message,
    encode_signal,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def raw(**over: object) -> RawMessage:
    base: dict[str, object] = {
        "user_id": "u",
        "mailbox_id": "m",
        "provider_message_id": "19e6d2b132fedfa8",
        "thread_id": "t1",
        "received_at": NOW,
        "headers": MessageHeaders(
            message_id="<1@x>",
            sender="Clara <clara@prima.it>",
            subject="Machine Learning Engineer",
            date="Thu, 30 Jul 2026 09:00:00 +0000",
        ),
        "text": "Hi Michele!",
        "body_sha256": "abc",
    }
    base.update(over)
    return RawMessage(**base)  # type: ignore[arg-type]


class TestTheMessageShapes:
    def test_a_raw_message_survives_the_round_trip(self) -> None:
        message = raw(
            invite=CalendarInvite(
                uid="ev-1", summary="System design", starts_at=NOW, organiser="clara@prima.it"
            )
        )
        assert decode_raw_message(encode_raw_message(message)) == message

    def test_the_from_header_keeps_its_name_on_the_wire(self) -> None:
        # `sender` is a Python concession; the queue and the reference both
        # call it `from`.
        payload = encode_raw_message(raw())
        assert payload["headers"]["from"] == "Clara <clara@prima.it>"
        assert "sender" not in payload["headers"]

    def test_a_backfilled_message_says_so_and_a_live_one_stays_quiet(self) -> None:
        assert "backfill" not in encode_raw_message(raw())
        assert encode_raw_message(raw(backfill=True))["backfill"] is True

    def test_a_candidate_is_flat_the_way_the_reference_spreads_it(self) -> None:
        candidate = CandidateMessage(
            message=raw(), score=5, cheap_only=False, reasons=("+3 ats",)
        )
        payload = encode_candidate_message(candidate)
        assert payload["provider_message_id"] == "19e6d2b132fedfa8"
        assert payload["score"] == 5
        assert decode_candidate_message(payload) == candidate

    def test_a_signal_survives_with_its_money_and_its_invite(self) -> None:
        signal = Signal(
            user_id="u",
            mailbox_id="m",
            provider_message_id="id",
            evidence_ref="id",
            intent="offer",
            occurred_at=NOW,
            confidence=0.94,
            rung=2,
            language="it",
            comp=Comp(currency="EUR", min_minor=5_500_000, max_minor=None),
            decide_by=NOW,
            invite=CalendarInvite(uid="ev-1", summary=None, starts_at=NOW),
        )
        assert decode_signal(encode_signal(signal)) == signal


class TestTheEventEnvelope:
    """Nested on the wire, flat in the dataclass."""

    def test_the_event_is_nested_where_the_reference_nests_it(self) -> None:
        payload = encode_pending_event(
            PendingEvent(
                user_id="u",
                application_id="a1",
                type="acknowledged",
                occurred_at=NOW,
                confidence=0.95,
                to_stage="acknowledged",
            )
        )
        assert set(payload) == {"user_id", "application_id", "event"}
        assert payload["event"]["type"] == "acknowledged"

    def test_an_absent_key_is_not_a_null_one(self) -> None:
        # `JSON.stringify` omits undefined and keeps null, so the reference
        # writes `to_stage: null` and no `from_stage` at all. Matching that is
        # what keeps the payloads identical while both implementations share a
        # queue.
        event = encode_pending_event(
            PendingEvent(
                user_id="u",
                application_id="a1",
                type="rejected",
                occurred_at=NOW,
                confidence=0.95,
            )
        )["event"]
        assert event["to_stage"] is None
        assert "from_stage" not in event

    def test_provenance_rides_along_only_when_there_is_some(self) -> None:
        with_source = encode_pending_event(
            PendingEvent(
                user_id="u",
                application_id="a1",
                type="applied",
                occurred_at=NOW,
                confidence=0.98,
                source=EventSource(channel="linkedin", is_first_touch=True),
            )
        )
        assert with_source["source"]["is_first_touch"] is True
        assert "source" not in encode_pending_event(
            PendingEvent(
                user_id="u",
                application_id="a1",
                type="applied",
                occurred_at=NOW,
                confidence=0.98,
            )
        )

    def test_it_reads_what_the_scheduler_writes(self) -> None:
        # `sweep_dormancy` and three other SQL functions build this shape with
        # `jsonb_build_object`. A decoder that expected the flat form would drop
        # every dormancy event and dead-letter it after five deliveries, which
        # looks exactly like dormancy quietly not working.
        from_sql = {
            "user_id": "019ffbc0-e82f-72ff-82f6-4186b4bbd7aa",
            "application_id": "019ffbc0-0000-72ff-82f6-000000000001",
            "event": {
                "type": "went_silent",
                "occurred_at": "2026-07-30T09:00:00+00:00",
                "confidence": 1.0,
                "rung": None,
                "payload": {"threshold_used": "p90x2", "presumed_closed": False},
            },
        }
        pending = decode_pending_event(from_sql)
        assert pending.type == "went_silent"
        assert pending.occurred_at == NOW
        assert pending.payload["threshold_used"] == "p90x2"

    def test_a_flat_payload_is_refused_rather_than_half_read(self) -> None:
        with pytest.raises(ValueError, match="nested"):
            decode_pending_event(
                {
                    "user_id": "u",
                    "application_id": "a1",
                    "type": "acknowledged",
                    "occurred_at": NOW.isoformat(),
                }
            )

    def test_a_round_trip_keeps_everything(self) -> None:
        pending = PendingEvent(
            user_id="u",
            application_id="a1",
            type="interview_scheduled",
            occurred_at=NOW,
            confidence=0.97,
            to_stage="interview",
            evidence_ref="msg-1",
            rung=2,
            payload={"stage": "interview", "status": "confirmed"},
            source=EventSource(channel="recruiter", posting_url=None, ats_vendor=None),
            silent=True,
        )
        assert decode_pending_event(encode_pending_event(pending)) == pending
