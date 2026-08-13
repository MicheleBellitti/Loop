"""The cheap filter, as a service.

The decision is in `loop.ladder.classify` and is pure; this fetches the four
things it may know beyond the message itself, runs it, and either passes the
message on or records that it did not.

A drop is written down, never deleted. It costs one row and it is the only
mechanism by which a false negative is ever discovered: the recall audit that
doubled extraction in P1 was an afternoon spent reading exactly these rows.
"""

import logging
import time
from dataclasses import dataclass
from typing import Final

from loop.db import Database, Message, publish
from loop.db.queue import Queue
from loop.db.seen import mark_seen
from loop.domain.wire import Json, decode_raw_message
from loop.ladder import ClassifierContext, Outcome, RuleRegistry, classify

from .consumer import Consumer, ConsumerOptions

# How long a user's context stands before it is read again. A minute is short
# enough that an application created now affects the next message but this one,
# and long enough that a backfill delivering thousands of messages does not run
# three queries per message.
CONTEXT_TTL_SECONDS: Final = 60.0

# Entries older than this are dropped on the next miss. The reference kept a
# `Map` that never evicted, which on a multi-tenant box is a set per user held
# for the life of the process.
_STALE_AFTER = 10


@dataclass(frozen=True, slots=True)
class Screened:
    """What one message was judged to be, so a caller can assert on it."""

    outcome: Outcome
    score: int
    reasons: tuple[str, ...]
    published: bool


class ClassifierService:
    def __init__(
        self,
        db: Database,
        *,
        registry: RuleRegistry | None = None,
        log: logging.Logger | None = None,
        ttl: float = CONTEXT_TTL_SECONDS,
    ) -> None:
        self._db = db
        self._registry = registry or RuleRegistry.load()
        self._log = log or logging.getLogger("loop.classifier")
        self._ttl = ttl
        self._contexts: dict[str, tuple[ClassifierContext, float]] = {}

    def consumer(self, options: ConsumerOptions | None = None) -> Consumer:
        # Twenty at a time: classification is a few milliseconds and the lease
        # is refreshed per message, so the batch costs nothing and a backfill
        # drains at a sensible rate.
        return Consumer(
            self._db,
            Queue.RAW,
            self.handle,
            options=options or ConsumerOptions(batch=20),
            log=self._log,
        )

    async def handle(self, message: Message) -> None:
        await self.screen(message.body)

    async def screen(self, body: Json) -> Screened:
        """Judge one message, and pass on the body it arrived in.

        The verdict is added to the payload rather than re-encoded from the
        decoded message, and that is deliberate: `encode_headers` writes the
        eight headers the classifier cares about, while a vendor rule may match
        on any header at all. Re-encoding here would amputate rung 1's header
        matching one hop before it runs, silently.
        """
        raw = decode_raw_message(body)
        context = await self.context_for(raw.user_id)
        verdict = classify(raw, context)

        published = verdict.outcome != "drop"
        async with self._db.session(raw.user_id) as connection:
            if published:
                await publish(
                    connection,
                    Queue.CANDIDATE,
                    {
                        **body,
                        "score": verdict.score,
                        "cheap_only": verdict.outcome == "cheap_only",
                        "reasons": list(verdict.reasons),
                    },
                )
            else:
                # `processed_at` is set here because a drop is the end of the
                # road. A message that passes leaves it null until a later stage
                # reaches a verdict, which is what the freshness alert reads.
                await mark_seen(connection, raw.mailbox_id, raw.provider_message_id, "dropped")

        self._log.info(
            "%s %s score=%d", verdict.outcome, raw.provider_message_id, verdict.score
        )
        return Screened(verdict.outcome, verdict.score, verdict.reasons, published)

    async def context_for(self, user_id: str) -> ClassifierContext:
        """Public because a replay needs exactly this and nothing else.

        A script that rebuilt the three queries itself would be a second copy
        of the definition of "a company this user already applied to", and the
        two would drift the first time either moved.
        """
        now = time.monotonic()
        cached = self._contexts.get(user_id)
        if cached is not None and now - cached[1] < self._ttl:
            return cached[0]

        self._evict(now)
        context = await self._load_context(user_id)
        # Populated only on success: a failed load must leave the cache empty so
        # the retry reads the database rather than a half-built context.
        self._contexts[user_id] = (context, now)
        return context

    def _evict(self, now: float) -> None:
        cutoff = self._ttl * _STALE_AFTER
        for user_id, (_context, loaded_at) in list(self._contexts.items()):
            if now - loaded_at > cutoff:
                del self._contexts[user_id]

    async def _load_context(self, user_id: str) -> ClassifierContext:
        """The three things the score depends on that are not in the message.

        One session for all three. The reference called `set_config(…, true)`
        outside a transaction, where Postgres scopes it to the single statement
        that follows — so its second and third queries ran with no tenant set at
        all and were saved only by connecting as a superuser.
        """
        async with self._db.session(user_id) as connection:
            companies = await connection.fetch(
                """
                select distinct c.domain
                  from companies c
                  join applications a on a.company_id = c.id
                 where a.user_id = $1 and c.domain is not null
                """,
                user_id,
            )
            threads = await connection.fetch(
                """
                select distinct payload->>'thread_id' as thread_id
                  from application_events
                 where user_id = $1 and payload ? 'thread_id'
                """,
                user_id,
            )
            newsletters = await connection.fetch(
                """
                select split_part(provider_message_id, '@', 2) as domain
                  from seen_messages
                 where user_id = $1 and outcome = 'dropped'
                 group by 1 having count(*) >= 5
                """,
                user_id,
            )

        return ClassifierContext(
            ats_domains=self._registry.ats_domains,
            # `companies.domain` is citext and `domain_of_address` returns lower
            # case; the comparison is a plain `in` against a Python set.
            company_domains=frozenset(
                row["domain"].lower() for row in companies if row["domain"]
            ),
            known_threads=frozenset(row["thread_id"] for row in threads if row["thread_id"]),
            # Empty on Gmail, and left that way on purpose. The query splits the
            # provider id on `@` expecting an RFC message-id, and Gmail's ids are
            # hex with no `@` in them, so every row yields "" and the filter
            # below empties the set. Fixing it needs a sender domain on
            # `seen_messages`, which the connector has at insert time — a schema
            # change, not a cleverer split, and not one to make while the
            # differential against the reference is what keeps this port honest.
            known_newsletters=frozenset(
                row["domain"] for row in newsletters if row["domain"]
            ),
        )
