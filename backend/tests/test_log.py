"""The log line, which is the one artefact this system deliberately keeps.

"…never subject lines, never sender addresses, never body fragments. That single
line is enough to debug extraction, which is the point of keeping it clean enough
to be safe to keep." (Spec §16)

Two rules, both enforced rather than documented: nothing that looks like a
secret survives, and a structured field not on the allow-list does not reach the
line at all.
"""

import io
import json
import logging

import pytest

from loop.runtime import ALLOWED_FIELDS, configure_logging, redact


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    yield stream
    logging.getLogger().handlers.clear()


@pytest.fixture
def logged_json(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    monkeypatch.setenv("LOG_FORMAT", "json")
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    yield stream
    logging.getLogger().handlers.clear()


class TestRedacting:
    def test_replaces_anything_that_looks_like_a_secret(self) -> None:
        scrubbed = redact(
            {
                "mailbox_id": "mb-1",
                "refresh_token": "1//0gTheWholeOfTheTrust",
                "app_password": "hunter2",
                "authorization": "Bearer ya29…",
                "access_key": "AKIA…",
                "session_cookie": "loop_session=…",
            }
        )
        assert scrubbed["mailbox_id"] == "mb-1"
        assert set(scrubbed) - {"mailbox_id"} == {
            "refresh_token",
            "app_password",
            "authorization",
            "access_key",
            "session_cookie",
        }
        assert all(v == "[redacted]" for k, v in scrubbed.items() if k != "mailbox_id")

    def test_reaches_into_nested_structures(self) -> None:
        scrubbed = redact({"mailbox": {"grants": [{"refresh_token": "…"}], "id": "mb-1"}})
        assert scrubbed["mailbox"]["grants"][0]["refresh_token"] == "[redacted]"
        assert scrubbed["mailbox"]["id"] == "mb-1"

    def test_a_container_named_like_a_secret_goes_whole(self) -> None:
        # `tokens` matches, so the list never gets walked. Redacting the
        # container rather than its contents is the safer way round: a shape
        # nobody anticipated cannot leak through it.
        assert redact({"tokens": [{"value": "…"}]})["tokens"] == "[redacted]"

    def test_keeps_the_key_so_the_line_still_says_what_was_there(self) -> None:
        # Dropping the key would make a log line that cannot tell you whether a
        # token was present at all, which is usually the question.
        assert "refresh_token" in redact({"refresh_token": "…"})

    def test_never_encodes_bytes(self) -> None:
        # The only bytes this system holds are a ciphertext or a key.
        assert redact({"dek_wrapped": b"\x00\x01"})["dek_wrapped"] == "[bytes]"

    def test_leaves_ordinary_values_alone(self) -> None:
        assert redact({"count": 3, "rung": 2, "ok": True, "at": None}) == {
            "count": 3,
            "rung": 2,
            "ok": True,
            "at": None,
        }


class TestTheLine:
    def test_a_secret_in_an_interpolated_argument_never_reaches_it(
        self, logged: io.StringIO
    ) -> None:
        row = {"mailbox_id": "mb-1", "refresh_token": "1//0gTheWholeOfTheTrust"}
        logging.getLogger("loop.connector").info("sync failed for %s", row)

        line = logged.getvalue()
        assert "1//0gTheWholeOfTheTrust" not in line
        assert "[redacted]" in line
        assert "mb-1" in line

    def test_a_secret_in_a_structured_field_never_reaches_it(
        self, logged_json: io.StringIO
    ) -> None:
        # Two mechanisms, and this exercises both: `refresh_token` is not on the
        # allow-list so it never arrives, and the `grant` that is allow-listed
        # would still have been scrubbed on the way through had it been.
        logging.getLogger("loop.connector").info(
            "synced",
            extra={
                "mailbox_id": "mb-1",
                "refresh_token": "1//0gTheWholeOfTheTrust",
                "reason": {"authorization": "Bearer ya29…"},
            },
        )
        line = json.loads(logged_json.getvalue())
        assert line["mailbox_id"] == "mb-1"
        assert "refresh_token" not in line
        assert line["reason"] == {"authorization": "[redacted]"}

    def test_an_allow_listed_field_is_readable_because_that_is_what_the_list_means(
        self, logged_json: io.StringIO
    ) -> None:
        # `sync_token` matches the secret pattern and is on the list anyway: it
        # is an opaque cursor, debugging a history sync needs it, and a name on
        # the allow-list has been looked at. The list is the authority for a
        # top-level field; `redact` is the authority for everything under one.
        logging.getLogger("loop.connector").info("synced", extra={"sync_token": "CAISBB…"})
        assert json.loads(logged_json.getvalue())["sync_token"] == "CAISBB…"

    def test_a_field_not_on_the_allow_list_is_dropped(
        self, logged_json: io.StringIO
    ) -> None:
        # `subject` is the field this rule exists for. Adding it is a change
        # someone has to make in `loop/runtime/log.py`, in a diff.
        assert "subject" not in ALLOWED_FIELDS
        logging.getLogger("loop.classifier").info(
            "scored", extra={"subject": "Re: your application", "score": 5}
        )
        line = json.loads(logged_json.getvalue())
        assert "subject" not in line
        assert line["score"] == 5

    def test_json_carries_the_four_fields_every_line_has(
        self, logged_json: io.StringIO
    ) -> None:
        logging.getLogger("loop.pipeline").info("appended")
        line = json.loads(logged_json.getvalue())
        assert line["level"] == "info"
        assert line["service"] == "loop.pipeline"
        assert line["msg"] == "appended"
        assert line["time"].startswith("20")

    def test_an_exception_is_carried_without_its_arguments_being_reinterpolated(
        self, logged: io.StringIO
    ) -> None:
        try:
            raise RuntimeError("connection refused")
        except RuntimeError:
            logging.getLogger("loop.connector").exception(
                "could not reach %s", {"authorization": "Bearer ya29…"}
            )
        line = logged.getvalue()
        assert "ya29" not in line
        assert "RuntimeError: connection refused" in line
