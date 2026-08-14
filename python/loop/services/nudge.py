"""What is true, once every fifteen minutes.

The four rules are in `loop.domain.nudges` and are pure; this reads the snapshot
they need, writes what they said, and hands the pushable ones to the notifier.
It is the one service that is not a queue consumer — `notify_pending` is the
queue it writes, and what wakes it is `listen loop_nudge`, which pg_cron fires.

The insert is `on conflict do nothing` and that single clause is the whole
enforcement of "one suggestion per application per rule, ever". Nothing in this
system ever updates a suggestion row after it lands, which has consequences the
sections below name rather than hide.
"""

import asyncio
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

import asyncpg

from loop.db import Database, load_stage_table, publish
from loop.db.queue import Queue
from loop.domain import iso_z
from loop.domain.messages import PendingNotification
from loop.domain.nudges import (
    AppSnapshot,
    DeadlineSnapshot,
    InterviewSnapshot,
    NudgeInput,
    Suggestion,
    evaluate_nudges,
)
from loop.domain.thresholds import TIME_IN_STAGE_MIN_TRANSITIONS
from loop.domain.wire import Json, encode_pending_notification

NUDGE_CHANNEL: Final = "loop_nudge"

# pg_cron sends the tick. This is the belt-and-braces interval behind it: if the
# schedule is wrong or the extension is missing, the product must still nudge,
# just less punctually.
TICK_INTERVAL_SECONDS: Final = 900


@dataclass(frozen=True, slots=True)
class Ticked:
    """What one tick did, so a test can assert on it."""

    users: int
    evaluated: int
    inserted: int
    notified: int


def suggestion_payload(suggestion: Suggestion) -> Json:
    """camelCase, and that is not a slip.

    `/api/today` spreads this column straight into its response and the client
    reads `applicationIds` off the result to decide what the card's button
    opens. Writing the dataclass field names instead would leave every
    suggestion card with a button that does nothing, and nothing anywhere would
    log an error.

    The timestamps are formatted here rather than left to the jsonb codec, whose
    fallback writes `+00:00` where every other timestamp in both implementations
    ends in `Z`.
    """
    return {
        "key": suggestion.key,
        "rule": suggestion.rule,
        "applicationIds": [str(a) for a in suggestion.application_ids],
        "kind": suggestion.kind,
        "meta": suggestion.meta,
        "title": suggestion.title,
        "body": suggestion.body,
        "cta": suggestion.cta,
        "expiresAt": iso_z(suggestion.expires_at),
        "urgencyAt": iso_z(suggestion.urgency_at),
        "depth": suggestion.depth,
        "pushable": suggestion.pushable,
        "bypassesBudget": suggestion.bypasses_budget,
    }


def notification_for(user_id: str, suggestion: Suggestion) -> PendingNotification:
    return PendingNotification(
        user_id=user_id,
        suggestion_key=suggestion.key,
        rule=suggestion.rule,
        title=suggestion.title,
        body=suggestion.body,
        url=f"/suggestions/{quote(suggestion.key, safe='')}",
        bypasses_budget=suggestion.bypasses_budget,
    )


class NudgeService:
    def __init__(self, db: Database, log: logging.Logger | None = None) -> None:
        self._db = db
        self._log = log or logging.getLogger("loop.nudge")
        self._stopping = asyncio.Event()
        # One tick at a time. The reference had no guard, so a burst of
        # notifications fanned out into concurrent per-user sessions and
        # correctness rested entirely on the unique constraint.
        self._ticking = asyncio.Lock()
        # Held so a tick a notification started is not garbage-collected mid-run.
        self._woken_tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """Tick now, then on every notification and every quarter of an hour.

        The listener needs a connection of its own — a pooled one would be
        handed to someone else while it was still subscribed — so this is the
        one place in the port that opens a connection outside the pool.
        """
        await self.tick()
        connection = await asyncpg.connect(self._db.dsn)
        try:
            await connection.add_listener(NUDGE_CHANNEL, self._woken)
            while not await self._sleep_until_due():
                await self._tick_quietly()
        finally:
            await connection.close()
            await asyncio.gather(*self._woken_tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopping.set()

    async def _sleep_until_due(self) -> bool:
        """Wait out the interval. True once someone has asked us to stop."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=TICK_INTERVAL_SECONDS)
        except TimeoutError:
            return False
        return True

    def _woken(self, *_notification: object) -> None:
        # asyncpg calls listeners synchronously, so a notification schedules a
        # tick rather than awaiting one. The lock in `tick` is what stops a
        # burst of them from running on top of each other.
        task = asyncio.create_task(self._tick_quietly())
        self._woken_tasks.add(task)
        task.add_done_callback(self._woken_tasks.discard)

    async def _tick_quietly(self) -> None:
        try:
            await self.tick()
        except Exception:
            self._log.exception("tick failed")

    async def tick(self, *, now: datetime | None = None) -> Ticked:
        """Everyone, serially. One user's failure is not the others' problem."""
        at = now or datetime.now(UTC)
        async with self._ticking:
            total = Ticked(0, 0, 0, 0)
            for user_id in await self._user_ids():
                total = _plus(total, await self.tick_user(user_id, now=at))
            return total

    async def tick_user(self, user_id: str, *, now: datetime | None = None) -> Ticked:
        """One user's evaluation, which is the whole unit of work.

        Public because it is the only thing here worth calling on its own — a
        replay, a test, a single account being investigated — and because
        `tick` reads better as a loop over it than as a loop with a body.
        """
        at = now or datetime.now(UTC)
        async with self._db.session(user_id) as connection:
            snapshot = await self._snapshot(connection, user_id, at)
            suggestions = evaluate_nudges(snapshot)
            fresh = await self._persist(connection, user_id, suggestions)
            # Published inside the same transaction as the insert, unlike the
            # reference, which published on the pool after the transaction had
            # committed. A crash in that gap lost the notification for good:
            # the next tick finds the key already open and never re-emits it.
            notified = 0
            for suggestion in fresh:
                if not suggestion.pushable:
                    continue
                await publish(
                    connection,
                    Queue.NOTIFY,
                    encode_pending_notification(notification_for(user_id, suggestion)),
                )
                notified += 1

        if suggestions:
            self._log.info(
                "%s evaluated=%d inserted=%d notified=%d",
                user_id,
                len(suggestions),
                len(fresh),
                notified,
            )
        return Ticked(1, len(suggestions), len(fresh), notified)

    async def _user_ids(self) -> list[str]:
        """Untenanted, necessarily: this is the query that finds the tenants.

        `users` has row-level security forced with a policy on its own primary
        key, so a tenant session — which is what every other query here uses —
        would return nobody and the service would nudge no one, quietly.
        """
        async with self._db.untenanted() as connection:
            rows = await connection.fetch("select id from users")
        return [str(row["id"]) for row in rows]

    async def _snapshot(
        self, connection: asyncpg.Connection, user_id: str, now: datetime
    ) -> NudgeInput:
        applications = await connection.fetch(
            """
            select a.id, c.canonical_name as company, a.role_title, a.current_stage,
                   a.status, a.last_signal_at, a.awaiting_them, a.last_user_action_at,
                   a.went_dormant_at
              from applications a
              join companies c on c.id = a.company_id
             where a.user_id = $1 and a.merged_into_id is null
            """,
            user_id,
        )
        # `$2`, not `now()`. Everything the rules decide is measured against the
        # instant `tick` pinned once for the whole run, and a snapshot filtered
        # by the server's clock is a different question asked at a different
        # moment: `tick(now=…)` — a replay, or a test — got rows selected by the
        # real clock and then judged against the given one, and even the
        # ordinary path drifted by however long the users ahead of this one took.
        interviews = await connection.fetch(
            """
            select id, application_id, stage, starts_at from interviews
             where user_id = $1 and cancelled_at is null and starts_at > $2
            """,
            user_id,
            now,
        )
        deadlines = await connection.fetch(
            """
            select application_id, kind, due_at, source from deadlines
             where user_id = $1 and met_at is null and due_at > $2
            """,
            user_id,
            now,
        )
        # `stage_dwell_in` is a plain view, so row-level security on the events
        # underneath it is evaluated as the view's owner and does not apply.
        # This `where` is the only tenant isolation on the query; without it
        # another user's percentiles set this user's follow-up thresholds.
        dwell = await connection.fetch(
            "select stage, p50_days, p75_days, n from stage_dwell_in where user_id = $1",
            user_id,
        )
        # Deliberately silent about `snoozed_until`, which the read path does
        # filter on. That asymmetry is the whole snooze mechanism: "Later" hides
        # the card for a day while the rule stays satisfied, so it comes back
        # without a second push.
        open_keys = await connection.fetch(
            """
            select key from suggestions
             where user_id = $1 and dismissed_at is null and acted_at is null
               and (expires_at is null or expires_at > $2)
            """,
            user_id,
            now,
        )
        stages = await load_stage_table(connection, user_id)

        gated = {
            row["stage"]: row for row in dwell if row["n"] >= TIME_IN_STAGE_MIN_TRANSITIONS
        }

        def p75(stage: str) -> float | None:
            row = gated.get(stage)
            return float(row["p75_days"]) if row else None

        def p50(stage: str) -> float | None:
            """Half-up, because the answer is shown to a human.

            Python's `round` is banker's rounding and `percentile_cont` produces
            exact halves routinely, so `round(2.5)` would tell the user their
            median wait is two days where the reference says three.
            """
            row = gated.get(stage)
            return None if row is None else float(math.floor(float(row["p50_days"]) + 0.5))

        return NudgeInput(
            now=now,
            applications=[_app(row) for row in applications],
            interviews=[_interview(row) for row in interviews],
            deadlines=[_deadline(row) for row in deadlines],
            p75_dwell_days=p75,
            p50_dwell_days=p50,
            open_or_issued=frozenset(row["key"] for row in open_keys),
            stages=stages,
        )

    async def _persist(
        self,
        connection: asyncpg.Connection,
        user_id: str,
        suggestions: Sequence[Suggestion],
    ) -> list[Suggestion]:
        """Insert what is new, and report only what actually landed.

        `on conflict do nothing` is the durable half of "one per application per
        rule, ever": a key that has been acted on, dismissed or expired keeps its
        row, so the rule re-firing on the next tick changes nothing. Which also
        means a suggestion does not come back after it expires — the spec's
        "unless it expired and re-triggered" is not what the implementation does,
        and making it true is a `do update` and a product decision, not a fix to
        slip into a port.
        """
        fresh: list[Suggestion] = []
        for suggestion in suggestions:
            landed = await connection.fetchval(
                """
                insert into suggestions
                  (user_id, key, rule, application_ids, payload, expires_at)
                values ($1,$2,$3,$4,$5,$6)
                on conflict (user_id, key) do nothing
                returning id
                """,
                user_id,
                suggestion.key,
                suggestion.rule,
                [str(a) for a in suggestion.application_ids],
                suggestion_payload(suggestion),
                suggestion.expires_at,
            )
            if landed is not None:
                fresh.append(suggestion)
        return fresh


def _plus(a: Ticked, b: Ticked) -> Ticked:
    return Ticked(
        a.users + b.users,
        a.evaluated + b.evaluated,
        a.inserted + b.inserted,
        a.notified + b.notified,
    )


def _app(row: asyncpg.Record) -> AppSnapshot:
    return AppSnapshot(
        id=str(row["id"]),
        company=row["company"],
        role_title=row["role_title"],
        current_stage=row["current_stage"],
        status=row["status"],
        last_signal_at=row["last_signal_at"],
        awaiting_them=row["awaiting_them"],
        last_user_action_at=row["last_user_action_at"],
        went_dormant_at=row["went_dormant_at"],
    )


def _interview(row: asyncpg.Record) -> InterviewSnapshot:
    return InterviewSnapshot(
        id=str(row["id"]),
        application_id=str(row["application_id"]),
        stage=row["stage"],
        starts_at=row["starts_at"],
    )


def _deadline(row: asyncpg.Record) -> DeadlineSnapshot:
    return DeadlineSnapshot(
        application_id=str(row["application_id"]),
        kind=row["kind"],
        due_at=row["due_at"],
        source=row["source"],
    )
