"""Whether the user may be interrupted.

The nudge service decides what is true. This decides whether it is worth a buzz,
and almost every line of it is a reason not to send: at most one push a calendar
day in the user's own timezone, nothing between 21:00 and 08:00, and one rule —
the deadline — allowed past both because a deadline you miss cannot be undone.

The two suppressions are not the same suppression. A quiet hour *defers*: the
message goes back on the queue with a delay and arrives at 08:00. The daily cap
*drops*: the second suggestion of a day is gone, however urgent, because order
of arrival decided. That asymmetry is inherited and worth knowing about.

The shape here is the runtime's, not the reference's. The TypeScript ran the
HTTPS call to a third-party push service inside the transaction that had claimed
the queue message — the same defect this whole port is organised around not
repeating, one hop further out.
"""

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from loop.db import Database, Message, publish
from loop.db.queue import Queue
from loop.domain import is_quiet_hour, next_deliverable_at
from loop.domain.messages import PendingNotification
from loop.domain.thresholds import (
    DEADLINE_BREAKS_QUIET_HOURS,
    MAX_PUSH_PER_DAY,
    QUIET_FROM,
    QUIET_TO,
)
from loop.domain.wire import decode_pending_notification, encode_pending_notification

from .consumer import Consumer, ConsumerOptions
from .push import PushPayload, PushSender, PushSubscription, VapidConfig, WebPushSender

# Belt and braces. `rejected` and `went_silent` are event types rather than
# nudge rules and cannot arrive here, but this is the line onboarding points at
# when it promises that a rejection never buzzes your phone, and a promise with
# no enforcement point is a comment.
NEVER_PUSHED = frozenset({"rejected", "went_silent", "let_it_go"})

# The owner's zone rather than UTC, because a missing `users.tz` on this product
# means the row was written before the column was, not that the user is in
# Greenwich.
_FALLBACK_TZ = "Europe/Rome"

Outcome = Literal["sent", "failed", "suppressed", "deferred", "no_subscription"]


@dataclass(frozen=True, slots=True)
class Delivered:
    outcome: Outcome
    count: int = 0
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Audience:
    """What one short read decided, so the sending needs no connection."""

    tz: str
    subscriptions: tuple[PushSubscription, ...]
    already_sent_today: int


class NotifierService:
    def __init__(
        self,
        db: Database,
        *,
        vapid: VapidConfig | None = None,
        sender: PushSender | None = None,
        quiet_hours: tuple[str, str] = (QUIET_FROM, QUIET_TO),
        log: logging.Logger | None = None,
    ) -> None:
        self._db = db
        self._sender = sender or WebPushSender(vapid or VapidConfig())
        self._quiet_hours = quiet_hours
        self._log = log or logging.getLogger("loop.notifier")

    def consumer(self, options: ConsumerOptions | None = None) -> Consumer:
        return Consumer(
            self._db,
            Queue.NOTIFY,
            self.handle,
            options=options or ConsumerOptions(batch=10),
            log=self._log,
        )

    async def handle(self, message: Message) -> None:
        await self.deliver(decode_pending_notification(message.body))

    async def deliver(
        self, pending: PendingNotification, *, now: datetime | None = None
    ) -> Delivered:
        at = now or datetime.now(UTC)

        if pending.rule in NEVER_PUSHED:
            return self._done(pending, Delivered("suppressed", reason="never pushed"))

        audience = await self._audience(pending, at)

        if self._over_the_cap(pending, audience):
            # Dropped, not deferred: the day's one slot is taken and tomorrow's
            # will belong to whatever is true tomorrow.
            return self._done(pending, Delivered("suppressed", reason="daily cap"))

        if self._must_wait(pending, audience.tz, at):
            return self._done(pending, await self._defer(pending, audience.tz, at))

        if not audience.subscriptions:
            return self._done(pending, Delivered("no_subscription"))

        delivered, gone = await self._send(pending, audience)

        if delivered or gone:
            await self._record(pending, audience, at, delivered=delivered, gone=gone)
        return self._done(
            pending, Delivered("sent" if delivered else "failed", count=delivered)
        )

    async def _audience(
        self, pending: PendingNotification, now: datetime
    ) -> _Audience:
        """One short read: who to send to, where they are, and what they have had.

        The cap query leans on row-level security, and does so in the direction
        that fails quietly — a session with no tenant set counts zero and the
        cap silently stops existing. `db.session` is the only thing standing
        between that and a phone buzzing all day.
        """
        async with self._db.session(pending.user_id) as connection:
            tz = await connection.fetchval(
                "select tz from users where id = $1", pending.user_id
            )
            tz = tz or _FALLBACK_TZ
            sent_today = await connection.fetchval(
                """
                select count(*) from notifications_sent
                 where user_id = $1 and local_date = $2
                """,
                pending.user_id,
                _local_date(now, tz),
            )
            rows = await connection.fetch(
                "select endpoint, p256dh, auth from push_subscriptions where user_id = $1",
                pending.user_id,
            )
        return _Audience(
            tz=tz,
            subscriptions=tuple(
                PushSubscription(r["endpoint"], r["p256dh"], r["auth"]) for r in rows
            ),
            already_sent_today=int(sent_today),
        )

    def _over_the_cap(self, pending: PendingNotification, audience: _Audience) -> bool:
        if pending.bypasses_budget:
            return False
        return audience.already_sent_today >= MAX_PUSH_PER_DAY

    def _must_wait(self, pending: PendingNotification, tz: str, now: datetime) -> bool:
        if not is_quiet_hour(now, tz, self._quiet_hours):
            return False
        return not (pending.bypasses_budget and DEADLINE_BREAKS_QUIET_HOURS)

    async def _defer(
        self, pending: PendingNotification, tz: str, now: datetime
    ) -> Delivered:
        """Back on the queue, timed to land when the quiet window ends.

        Note what the redelivered copy re-runs: every gate, including the cap
        against the *new* local date. A notification held overnight is dropped
        at 08:00 if something else has already taken the day's slot, so a
        deferral is not a promise.
        """
        deliverable_at = next_deliverable_at(now, tz, self._quiet_hours)
        delay = max(0, math.ceil((deliverable_at - now).total_seconds()))
        async with self._db.session(pending.user_id) as connection:
            await publish(
                connection, Queue.NOTIFY, encode_pending_notification(pending), delay=delay
            )
        return Delivered("deferred", reason="quiet hours")

    async def _send(
        self, pending: PendingNotification, audience: _Audience
    ) -> tuple[int, list[str]]:
        """Every device the user has, with no connection held for any of them."""
        payload = PushPayload(
            title=pending.title,
            body=pending.body,
            url=pending.url,
            # The dedup key, and it must stay the suggestion key: delivery is
            # at-least-once and this is the only thing that keeps a redelivery
            # from being a second buzz.
            tag=pending.suggestion_key,
        )
        delivered = 0
        gone: list[str] = []
        for subscription in audience.subscriptions:
            match await self._sender.send(subscription, payload):
                case "ok":
                    delivered += 1
                case "gone":
                    gone.append(subscription.endpoint)
                case "unconfigured":
                    self._log.warning("no VAPID keys; nothing was sent")
                case "failed":
                    self._log.warning("push rejected by %s", _host(subscription.endpoint))
        return delivered, gone

    async def _record(
        self,
        pending: PendingNotification,
        audience: _Audience,
        now: datetime,
        *,
        delivered: int,
        gone: list[str],
    ) -> None:
        """One row per notification, not per device — the cap counts occasions.

        A deadline that ignored the cap still writes here, so the exemption is
        one-way: a deadline may break the budget and it still spends it.
        """
        async with self._db.session(pending.user_id) as connection:
            if delivered:
                await connection.execute(
                    """
                    insert into notifications_sent
                      (user_id, rule, suggestion_key, local_date)
                    values ($1,$2,$3,$4)
                    """,
                    pending.user_id,
                    pending.rule,
                    pending.suggestion_key,
                    _local_date(now, audience.tz),
                )
            for endpoint in gone:
                await connection.execute(
                    "delete from push_subscriptions where user_id = $1 and endpoint = $2",
                    pending.user_id,
                    endpoint,
                )

    def _done(self, pending: PendingNotification, result: Delivered) -> Delivered:
        self._log.info(
            "%s %s rule=%s%s",
            result.outcome,
            pending.suggestion_key,
            pending.rule,
            f" ({result.reason})" if result.reason else "",
        )
        return result


def _local_date(now: datetime, tz: str) -> date:
    """The calendar day in the user's own zone.

    23:30 UTC is already tomorrow in Rome, and the cap is a promise about days
    as the user experiences them.
    """
    return now.astimezone(ZoneInfo(tz)).date()


def _host(endpoint: str) -> str:
    return urlsplit(endpoint).netloc or endpoint
