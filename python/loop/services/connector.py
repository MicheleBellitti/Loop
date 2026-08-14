"""Reading the mailbox, which is the only input this system has.

Three paths into the same ingest. A history sync since the stored cursor, woken
by a push notification or by a five-minute poll; a backfill over a window the
user chose; and a relist when the cursor has gone stale. All three end at
`_ingest`, which fetches the message, hydrates the invitation, normalises it and
publishes — once, because `seen_messages` remembers.

The cursor is the whole design. It advances only after the batch has been
published, so the box can be down for a day and lose nothing but timeliness, and
a crash mid-batch re-reads messages the replay log then drops.

**Establishing a cursor is not a backfill.** With no cursor at all this asks
Gmail for a history id and stops, reading no mail. The poll fires seconds after
the OAuth callback, long before the user has reached the "how far back?"
question, and a sync that read anything at that moment would make that choice a
no-op. The window the user picks is the only thing that decides how far back
Loop reads.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import asyncpg

from loop.connector.normalise import to_raw_message
from loop.db import Database, publish
from loop.db.queue import Queue
from loop.domain.thresholds import (
    BACKFILL_BATCH,
    BACKFILL_CONCURRENCY,
    MAX_BACKFILL_MONTHS,
    RELIST_DAYS,
    WATCH_RENEW_FAILURES_BEFORE_POLLING,
)
from loop.domain.wire import encode_raw_message
from loop.google.client import (
    GoogleAuthError,
    GoogleClient,
    GoogleRateLimit,
    HistoryTooOld,
    SyncTokenExpired,
)
from loop.google.mailbox import (
    Mailbox,
    mark_error,
    mark_needs_reauth,
    mark_ok,
    read_refresh_token,
    save_cursor,
    save_watch_expiry,
    set_backlog,
    to_mailbox,
)

_DAYS_PER_MONTH: Final = 30
_CALENDAR_FIRST_WINDOW_DAYS: Final = 90

_MAILBOXES = """
select id, user_id, provider, address, secret_ciphertext, secret_nonce,
       dek_wrapped, dek_nonce, scopes, cursor, watch_expires_at, status, last_ok_at
  from mailbox_accounts
 where provider = 'gmail' and status in ('ok', 'error')
"""


@dataclass(frozen=True, slots=True)
class Synced:
    """What one pass over one mailbox did."""

    read: int
    skipped: int
    outcome: str


class ConnectorService:
    def __init__(
        self,
        db: Database,
        client: GoogleClient,
        *,
        pubsub_topic: str | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._db = db
        self._google = client
        self._topic = pubsub_topic
        self._log = log or logging.getLogger("loop.connector")
        # Per mailbox, in memory: how many watch renewals have failed in a row.
        # A column would survive a restart, and a restart is exactly when it
        # should be forgotten — the thing that was failing may well be fixed.
        self._watch_failures: dict[str, int] = {}

    async def sync_all(self) -> dict[str, Synced]:
        """Every connected mailbox, one at a time.

        Serial rather than concurrent: this is one person's mail, and a
        connector that finishes a minute sooner is worth nothing next to one
        whose failures are easy to read in order.
        """
        results: dict[str, Synced] = {}
        for mailbox in await self._mailboxes():
            results[mailbox.id] = await self.sync(mailbox)
        return results

    async def sync(self, mailbox: Mailbox) -> Synced:
        try:
            token = await self._token(mailbox)
        except GoogleAuthError as error:
            return await self._auth_failed(mailbox, error)

        try:
            read = await self._sync_history(mailbox, token)
        except HistoryTooOld:
            # F2 — degraded and self-healing. The cursor is older than Gmail's
            # memory, so it relists a month and, unlike the reference,
            # re-establishes the cursor afterwards. Without that the stale id
            # stays in the row and every poll for the rest of time 404s and
            # relists the same thirty days again.
            self._log.warning(
                "history expired for %s; relisting %d days", mailbox.id, RELIST_DAYS
            )
            read = await self._relist(mailbox, token, RELIST_DAYS)
            await self._establish_cursor(mailbox, token)
        except GoogleAuthError as error:
            return await self._auth_failed(mailbox, error)
        except (GoogleRateLimit, RuntimeError) as error:
            async with self._db.session(mailbox.user_id) as connection:
                await mark_error(connection, mailbox.id, str(error))
            raise

        await self._sync_calendar(mailbox, token)
        async with self._db.session(mailbox.user_id) as connection:
            await mark_ok(connection, mailbox.id)
        return Synced(read=read, skipped=0, outcome="synced")

    async def backfill(self, mailbox: Mailbox, months: int) -> Synced:
        """The window the user chose, and the cursor moved before the scan.

        Establishing the cursor first means anything arriving *during* the scan
        is picked up by the next history sync rather than falling between the
        two.
        """
        token = await self._token(mailbox)
        await self._establish_cursor(mailbox, token)

        capped = max(1, min(months, MAX_BACKFILL_MONTHS))
        since = datetime.now(UTC) - timedelta(days=capped * _DAYS_PER_MONTH)
        return await self._run_query(mailbox, token, f"after:{since:%Y/%m/%d}")

    async def renew_watch(self, mailbox: Mailbox) -> bool:
        """Ask Gmail to keep pushing. Never raises.

        A watch lasts seven days and this runs daily, so there is a lot of room
        before silence. After three consecutive failures the mailbox is marked
        in error — not to stop it, but so the health line says why it has become
        slow, because the five-minute poll carries on regardless. That is the
        whole of F2: a lapsed subscription degrades to slow rather than silent.
        """
        if not self._topic:
            return False
        try:
            token = await self._token(mailbox)
            watch = await self._google.watch(token, self._topic)
        except (GoogleAuthError, GoogleRateLimit, RuntimeError, HistoryTooOld) as error:
            failures = self._watch_failures.get(mailbox.id, 0) + 1
            self._watch_failures[mailbox.id] = failures
            self._log.warning("watch renewal failed for %s: %s", mailbox.id, error)
            if failures >= WATCH_RENEW_FAILURES_BEFORE_POLLING:
                async with self._db.session(mailbox.user_id) as connection:
                    await mark_error(
                        connection,
                        mailbox.id,
                        f"watch renewal failed {failures}×; polling instead",
                    )
            return False

        self._watch_failures[mailbox.id] = 0
        async with self._db.session(mailbox.user_id) as connection:
            await save_watch_expiry(connection, mailbox.id, str(watch["expiration"]))
        return True

    # ── the three paths ─────────────────────────────────────────────────────

    async def _sync_history(self, mailbox: Mailbox, token: str) -> int:
        cursor = mailbox.cursor.get("historyId")
        if not cursor:
            await self._establish_cursor(mailbox, token)
            self._log.info("cursor established for %s; read nothing", mailbox.id)
            return 0

        message_ids: set[str] = set()
        latest = cursor
        page_token: str | None = None
        while True:
            page = await self._google.history(token, cursor, page_token)
            for record in page.get("history") or ():
                for added in record.get("messagesAdded") or ():
                    message_id = (added.get("message") or {}).get("id")
                    if message_id:
                        message_ids.add(message_id)
            # A quiet mailbox returns no history id; keeping the old one means
            # the cursor stands rather than resetting.
            latest = page.get("historyId") or latest
            page_token = page.get("nextPageToken")
            if not page_token:
                break

        for message_id in message_ids:
            await self._ingest(mailbox, token, message_id)

        # Only now. A crash before this line re-reads the batch, which
        # `seen_messages` makes free.
        async with self._db.session(mailbox.user_id) as connection:
            await save_cursor(connection, mailbox.id, {"historyId": latest})
        return len(message_ids)

    async def _relist(self, mailbox: Mailbox, token: str, days: int) -> int:
        since = datetime.now(UTC) - timedelta(days=days)
        result = await self._run_query(mailbox, token, f"after:{since:%Y/%m/%d}")
        return result.read

    async def _run_query(self, mailbox: Mailbox, token: str, query: str) -> Synced:
        read = skipped = 0
        page_token: str | None = None
        try:
            while True:
                page = await self._google.list_messages(
                    token, query, page_token, BACKFILL_BATCH
                )
                ids = [m["id"] for m in page.get("messages") or ()]
                # Per page, because `read` and `skipped` accumulate across pages
                # and `ids` does not: subtracting the running totals from one
                # page's length goes negative from page two onward.
                done = 0
                for slice_start in range(0, len(ids), BACKFILL_CONCURRENCY):
                    batch = ids[slice_start : slice_start + BACKFILL_CONCURRENCY]
                    outcomes = await asyncio.gather(
                        *(self._ingest(mailbox, token, i, backfill=True) for i in batch),
                        return_exceptions=True,
                    )
                    for message_id, outcome in zip(batch, outcomes, strict=True):
                        if isinstance(outcome, BaseException):
                            # One unreadable message must not end a scan of twelve
                            # months.
                            self._log.warning("skipped %s: %s", message_id, outcome)
                            skipped += 1
                        else:
                            read += 1
                        done += 1
                    await self._report_progress(mailbox, read, len(ids) - done)

                page_token = page.get("nextPageToken")
                if not page_token:
                    break
        finally:
            # Whether the scan finished or died on the way, nothing is queued
            # behind it any more. A backlog left standing here is an "F2 · still
            # catching up" strip that no later code path can ever clear.
            async with self._db.session(mailbox.user_id) as connection:
                await set_backlog(connection, mailbox.id, 0)
        return Synced(read=read, skipped=skipped, outcome="scanned")

    async def _sync_calendar(self, mailbox: Mailbox, token: str) -> None:
        """Invitations, which are the cheapest certain evidence there is.

        A calendar entry from a company already in the pipeline says an
        interview was scheduled with more confidence than any sentence in any
        email, and it says when.
        """
        sync_token = mailbox.cursor.get("syncToken")
        time_min = None if sync_token else _window_start()
        try:
            latest = await self._page_calendar(token, sync_token, time_min)
        except SyncTokenExpired:
            # Google forgets an incremental token eventually. The reference let
            # this throw on every run for ever, because nothing cleared the
            # dead token from the row.
            self._log.info("calendar sync token expired for %s; starting again", mailbox.id)
            latest = await self._page_calendar(token, None, _window_start())
        except (GoogleAuthError, GoogleRateLimit, RuntimeError, HistoryTooOld) as error:
            # A calendar that will not answer must not stop the mail being read.
            self._log.warning("calendar sync failed for %s: %s", mailbox.id, error)
            return

        if latest:
            async with self._db.session(mailbox.user_id) as connection:
                await save_cursor(connection, mailbox.id, {"syncToken": latest})

    async def _page_calendar(
        self, token: str, sync_token: str | None, time_min: str | None
    ) -> str | None:
        page_token: str | None = None
        latest: str | None = None
        while True:
            page = await self._google.list_calendar_events(
                token, sync_token=sync_token, time_min=time_min, page_token=page_token
            )
            latest = page.get("nextSyncToken") or latest
            page_token = page.get("nextPageToken")
            if not page_token:
                return latest

    # ── the shared ingest ───────────────────────────────────────────────────

    async def _ingest(
        self, mailbox: Mailbox, token: str, message_id: str, *, backfill: bool = False
    ) -> None:
        """Fetch, hydrate, normalise, record, publish — in that order.

        The `seen_messages` insert is what makes the whole pipeline replayable:
        it is written before the message is published, so a redelivery finds the
        row and publishes nothing, and a message can be re-read from the
        provider by id for as long as the account exists.
        """
        async with self._db.session(mailbox.user_id) as connection:
            already = await connection.fetchval(
                """
                select 1 from seen_messages
                 where mailbox_id = $1 and provider_message_id = $2
                """,
                mailbox.id,
                message_id,
            )
        if already:
            return

        message = await self._google.hydrate_calendar_parts(
            token, await self._google.get_message(token, message_id)
        )
        raw = to_raw_message(
            message, user_id=mailbox.user_id, mailbox_id=mailbox.id, backfill=backfill
        )

        async with self._db.session(mailbox.user_id) as connection:
            await connection.execute(
                """
                insert into seen_messages
                  (mailbox_id, provider_message_id, user_id, body_sha256, received_at)
                values ($1,$2,$3,$4,$5)
                on conflict do nothing
                """,
                mailbox.id,
                raw.provider_message_id,
                mailbox.user_id,
                bytes.fromhex(raw.body_sha256),
                raw.received_at,
            )
            await publish(connection, Queue.RAW, encode_raw_message(raw))

    async def _report_progress(self, mailbox: Mailbox, read: int, remaining: int) -> None:
        """The scan is the one long wait in the product, so it is on screen."""
        async with self._db.untenanted() as connection:
            await connection.execute(
                "select pg_notify('loop_events', $1)",
                json.dumps(
                    {
                        "type": "scan.progress",
                        "user_id": mailbox.user_id,
                        "read": read,
                        "remaining": max(0, remaining),
                    }
                ),
            )
        async with self._db.session(mailbox.user_id) as connection:
            await set_backlog(connection, mailbox.id, max(0, remaining))

    # ── plumbing ────────────────────────────────────────────────────────────

    async def _mailboxes(self) -> list[Mailbox]:
        """Untenanted: this is the query that finds out whose mailboxes exist."""
        async with self._db.untenanted() as connection:
            rows = await connection.fetch(_MAILBOXES)
        return [to_mailbox(row) for row in rows]

    async def _token(self, mailbox: Mailbox) -> str:
        return await self._google.access_token(mailbox.id, read_refresh_token(mailbox))

    async def _establish_cursor(self, mailbox: Mailbox, token: str) -> None:
        profile = await self._google.profile(token)
        async with self._db.session(mailbox.user_id) as connection:
            await save_cursor(connection, mailbox.id, {"historyId": profile["historyId"]})

    async def _auth_failed(self, mailbox: Mailbox, error: GoogleAuthError) -> Synced:
        self._google.forget_token(mailbox.id)
        async with self._db.session(mailbox.user_id) as connection:
            if error.needs_reauth:
                await mark_needs_reauth(connection, mailbox.id, str(error))
            else:
                await mark_error(connection, mailbox.id, str(error))
        self._log.warning("auth failed for %s: %s", mailbox.id, error)
        outcome = "needs_reauth" if error.needs_reauth else "error"
        return Synced(read=0, skipped=0, outcome=outcome)


def _window_start() -> str:
    """How far back a first calendar read looks, in the format Google wants."""
    since = datetime.now(UTC) - timedelta(days=_CALENDAR_FIRST_WINDOW_DAYS)
    return f"{since:%Y-%m-%dT%H:%M:%SZ}"


async def mailbox_by_id(
    connection: asyncpg.Connection, mailbox_id: str
) -> Mailbox | None:
    row = await connection.fetchrow(f"{_MAILBOXES} and id = $1", mailbox_id)
    return to_mailbox(row) if row else None
