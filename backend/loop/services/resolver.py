"""The resolver: which application a signal is about.

Concurrency 1. Ordering matters, and two signals arriving together must not both
decide "there is no application at this company yet" and create one each. That
constraint is the reason this is its own process.

The decisions themselves are not here — they are in `loop.resolver`, pure, and
tested without a database. This is the shell that fetches what a decision needs,
carries it out, and writes down what it said. The split is what lets the
thresholds a wrong merge turns on be exercised in a tenth of a second, and it is
what will let the model in P4 sit between two short transactions.
"""

import logging
from dataclasses import dataclass

import asyncpg

from loop.db import Database, Message, publish
from loop.db.queue import Queue
from loop.db.seen import Outcome, mark_seen
from loop.domain import normalise_role
from loop.domain.messages import Signal
from loop.domain.thresholds import MERGE_UNDO_DAYS
from loop.domain.wire import decode_signal, encode_pending_event
from loop.ladder import RuleRegistry
from loop.resolver import (
    Ambiguous,
    Candidate,
    Created,
    Embedder,
    Merge,
    create_embedder,
    decide,
    events_for_signal,
    find_duplicate,
    parse_vector,
    plan_lookup,
    role_facts,
    to_vector,
)

from .consumer import Consumer, ConsumerOptions

_CANCELLED_INTERVIEW_QUESTION = (
    "An interview was cancelled. Rescheduling, or has this one ended?"
)


@dataclass(frozen=True, slots=True)
class Resolved:
    """What one signal did, so a caller can assert on it."""

    application_id: str | None
    outcome: str
    events: int
    merged: Merge | None = None


class ResolverService:
    def __init__(
        self,
        db: Database,
        *,
        registry: RuleRegistry | None = None,
        embedder: Embedder | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._db = db
        self._registry = registry or RuleRegistry.load()
        # `create_embedder`, not `LexicalEmbedder()`: it is the only thing that
        # reads `EMBEDDING_MODEL`, and hardcoding the lexical stand-in here made
        # the documented switch a setting that changed nothing — no error, no
        # log line, still 384-dimensional hashes.
        self._embedder = embedder or create_embedder()
        self._log = log or logging.getLogger("loop.resolver")

    def consumer(self, options: ConsumerOptions | None = None) -> Consumer:
        # Batch of one: the whole point of this service is that it decides one
        # signal at a time.
        return Consumer(
            self._db,
            Queue.SIGNAL,
            self.handle,
            options=options or ConsumerOptions(batch=1),
            log=self._log,
        )

    async def handle(self, message: Message) -> None:
        await self.resolve(decode_signal(message.body))

    async def resolve(self, signal: Signal) -> Resolved:
        async with self._db.session(signal.user_id) as connection:
            application_id = await self._identify_by_thread(connection, signal)

            if application_id is None:
                company_id = await self._canonicalise_company(connection, signal)
                embedding = self._embed(signal)
                decision = decide(
                    signal, embedding, await self._candidates(connection, company_id, signal)
                )

                if isinstance(decision, Ambiguous):
                    await self._ask_which(connection, signal, decision)
                    await self._mark_seen(connection, signal, "review")
                    return Resolved(None, "review", 0)

                if isinstance(decision, Created):
                    application_id = await self._create(
                        connection, signal, company_id, embedding
                    )
                else:
                    application_id = decision.application_id

                merged = await self._merge_a_duplicate(connection, signal, application_id)
                if merged is not None and merged.merge == application_id:
                    # The row this signal just landed on is the one that got
                    # merged away — which is the normal case, not the odd one:
                    # a row `_create` just inserted has no `applied_at`, and
                    # `find_duplicate` keeps the earlier of the two. Everything
                    # downstream filters on `merged_into_id is null`, so events
                    # left pointing here would land on a row the board, the
                    # candidate search and the nudge snapshot all hide.
                    application_id = merged.keep
            else:
                merged = None

            events = events_for_signal(signal, application_id)
            for event in events:
                await publish(connection, Queue.EVENT, encode_pending_event(event))

            if signal.intent == "interview_cancelled":
                # Ambiguous by nature: rescheduling, or over? The stage claim is
                # withdrawn automatically; the question is asked once.
                await self._raise_review(
                    connection,
                    signal,
                    kind="unknown_intent",
                    application_id=application_id,
                    excerpt=_CANCELLED_INTERVIEW_QUESTION,
                )

            outcome: Outcome = "placed" if events else "dropped"
            await self._mark_seen(connection, signal, outcome)

        self._log.info(
            "%s %s → %s (%s events)",
            signal.intent,
            signal.provider_message_id,
            outcome,
            len(events),
        )
        return Resolved(application_id, outcome, len(events), merged)

    # ── identity ────────────────────────────────────────────────────────────

    async def _identify_by_thread(
        self, connection: asyncpg.Connection, signal: Signal
    ) -> str | None:
        """The cheapest and strongest signal there is, and the first one tried."""
        if signal.application_hint:
            return signal.application_hint
        if not signal.thread_id:
            return None
        found = await connection.fetchval(
            """
            select application_id from application_events
             where user_id = $1 and payload->>'thread_id' = $2 limit 1
            """,
            signal.user_id,
            signal.thread_id,
        )
        return str(found) if found else None

    async def _canonicalise_company(
        self, connection: asyncpg.Connection, signal: Signal
    ) -> str:
        """Domain first, then the alias, then create — and record every spelling.

        The order is the policy in `loop.resolver.company`; the lookups are here.
        "ION Group" arriving from an ATS display name and "iongroup" derived from
        the company's own domain have to land on one row, or the pipeline forks
        in two and so do the statistics.
        """
        plan = plan_lookup(signal, self._registry.ats_domains)

        if plan.domain:
            by_domain = await connection.fetchval(
                "select id from companies where domain = $1", plan.domain
            )
            if by_domain:
                return str(by_domain)

        if plan.alias:
            by_alias = await connection.fetchval(
                "select company_id from company_aliases where user_id = $1 and alias = $2",
                signal.user_id,
                plan.alias,
            )
            if by_alias:
                return str(by_alias)

            by_name = await connection.fetchval(
                """
                select id from companies
                 where regexp_replace(lower(canonical_name), '[^a-z0-9]+', '', 'g') = $1
                 limit 1
                """,
                plan.alias,
            )
            if by_name:
                await self._record_aliases(connection, signal.user_id, str(by_name), plan.alias)
                return str(by_name)

        company_id = str(
            await connection.fetchval(
                """
                insert into companies (canonical_name, domain) values ($1, $2)
                on conflict (lower(canonical_name), coalesce(domain, '')) do update
                  set canonical_name = excluded.canonical_name
                returning id
                """,
                plan.name,
                plan.domain,
            )
        )
        await self._record_aliases(
            connection, signal.user_id, company_id, *plan.aliases_to_record
        )
        return company_id

    async def _record_aliases(
        self, connection: asyncpg.Connection, user_id: str, company_id: str, *aliases: str
    ) -> None:
        for alias in aliases:
            await connection.execute(
                """
                insert into company_aliases (user_id, company_id, alias) values ($1,$2,$3)
                on conflict do nothing
                """,
                user_id,
                company_id,
                alias,
            )

    # ── matching ────────────────────────────────────────────────────────────

    def _embed(self, signal: Signal) -> list[float]:
        return self._embedder.embed(signal.role_normalised or signal.role or "unknown")

    async def _candidates(
        self, connection: asyncpg.Connection, company_id: str, signal: Signal
    ) -> list[Candidate]:
        rows = await connection.fetch(
            """
            select a.id, a.role_embedding::text as role_embedding, a.applied_at, a.status,
                   a.current_stage, a.work_mode, a.location, a.manually_created,
                   coalesce(split.ids, '{}') as split_from
              from applications a
              left join lateral (
                -- `merged_id`, not `to`. The filter below pins `to` to the
                -- literal 'split', so aggregating it gives every candidate the
                -- string 'split' and never an id — and `matching.py` compares
                -- ids, so the guard could never fire and a pair the user pulled
                -- apart would silently merge again. The id of the row that was
                -- freed is what `review.py` puts in `merged_id`.
                select array_agg(e.payload->>'merged_id') as ids
                  from application_events e
                 where e.application_id = a.id
                   and e.type = 'human_corrected'
                   and e.payload->>'field' = 'merge'
                   and e.payload->>'to' = 'split'
                   and e.payload->>'merged_id' is not null
              ) split on true
             where a.user_id = $1 and a.company_id = $2 and a.merged_into_id is null
               and a.status in ('live','dormant')
            """,
            signal.user_id,
            company_id,
        )
        return [
            Candidate(
                id=str(row["id"]),
                embedding=parse_vector(row["role_embedding"]),
                applied_at=row["applied_at"],
                status=row["status"],
                current_stage=row["current_stage"],
                work_mode=row["work_mode"],
                location=row["location"],
                manually_created=row["manually_created"],
                split_from=frozenset(row["split_from"] or ()),
            )
            for row in rows
        ]

    async def _create(
        self,
        connection: asyncpg.Connection,
        signal: Signal,
        company_id: str,
        embedding: list[float],
    ) -> str:
        normalised = normalise_role(signal.role) if signal.role else None
        # The same three the event payload carries, from the same function, so
        # the row and the log cannot disagree about what a rebuild should find.
        facts = role_facts(signal)
        return str(
            await connection.fetchval(
                """
                insert into applications
                  (user_id, company_id, role_title, role_normalised, role_embedding,
                   seniority, location, work_mode, current_stage, current_phase, confidence)
                values ($1,$2,$3,$4,$5::vector,$6,$7,$8,'applied','sent',$9)
                returning id
                """,
                signal.user_id,
                company_id,
                (signal.role or "").strip() or "Unknown role",
                signal.role_normalised or (normalised.role if normalised else None),
                to_vector(embedding),
                facts.seniority,
                facts.location,
                facts.work_mode,
                signal.confidence,
            )
        )

    async def _merge_a_duplicate(
        self, connection: asyncpg.Connection, signal: Signal, application_id: str
    ) -> Merge | None:
        """The same job found twice is one application with two provenances.

        Automatic, because always asking would flood the queue with cases the
        resolver is right about — and reversible for a fortnight, because a
        silent irreversible merge is the failure the design fears most.
        """
        me = await self._one_candidate(connection, application_id)
        if me is None or not me.embedding:
            return None
        company_id = await connection.fetchval(
            "select company_id from applications where id = $1", application_id
        )
        others = [
            c
            for c in await self._candidates(connection, str(company_id), signal)
            if c.id != application_id
        ]
        merge = find_duplicate(me, others)
        if merge is None:
            return None

        await connection.execute(
            "update applications set merged_into_id = $1 where id = $2", merge.keep, merge.merge
        )
        await connection.execute(
            "update sources set application_id = $1 where application_id = $2",
            merge.keep,
            merge.merge,
        )
        await connection.execute(
            """
            insert into review_items
              (user_id, kind, evidence_ref, application_id, candidates, expires_at)
            values ($1, 'merge_undo', $2, $3, $4, now() + make_interval(days => $5))
            """,
            signal.user_id,
            signal.evidence_ref,
            merge.keep,
            [{"merged": merge.merge, "kept": merge.keep, "cosine": merge.cosine}],
            MERGE_UNDO_DAYS,
        )
        self._log.info("merged %s into %s at %.3f", merge.merge, merge.keep, merge.cosine)
        return merge

    async def _one_candidate(
        self, connection: asyncpg.Connection, application_id: str
    ) -> Candidate | None:
        row = await connection.fetchrow(
            """
            select id, role_embedding::text as role_embedding, applied_at, status,
                   current_stage, work_mode, location, manually_created
              from applications where id = $1
            """,
            application_id,
        )
        if row is None:
            return None
        return Candidate(
            id=str(row["id"]),
            embedding=parse_vector(row["role_embedding"]),
            applied_at=row["applied_at"],
            status=row["status"],
            current_stage=row["current_stage"],
            work_mode=row["work_mode"],
            location=row["location"],
            manually_created=row["manually_created"],
        )

    # ── asking a human ──────────────────────────────────────────────────────

    async def _ask_which(
        self, connection: asyncpg.Connection, signal: Signal, decision: Ambiguous
    ) -> None:
        """Two candidates within a hair of each other, so the system asks.

        One tap to confirm, and the answer is written back as a rule rather than
        as a one-off correction.
        """
        by_id = dict(decision.candidates)
        rows = await connection.fetch(
            """
            select id, role_title, current_stage, applied_at
              from applications where id = any($1::uuid[])
            """,
            list(by_id),
        )
        candidates = [
            {
                "application_id": str(row["id"]),
                "role_title": row["role_title"],
                "stage": row["current_stage"],
                "applied_at": row["applied_at"].isoformat() if row["applied_at"] else None,
                "cosine": by_id.get(str(row["id"]), 0.0),
            }
            for row in rows
        ]
        await self._raise_review(
            connection,
            signal,
            kind="ambiguous_match",
            excerpt=signal.excerpt,
            candidates=candidates,
        )

    async def _raise_review(
        self,
        connection: asyncpg.Connection,
        signal: Signal,
        *,
        kind: str,
        application_id: str | None = None,
        excerpt: str | None = None,
        candidates: list[dict[str, object]] | None = None,
    ) -> None:
        # `where not exists`, not `on conflict do nothing`: this table's only
        # unique constraint is a generated primary key, so there is nothing for
        # a conflict to arc across and a redelivered signal asked the same
        # question twice. `extractor.py` documents the same fix.
        await connection.execute(
            """
            insert into review_items
              (user_id, kind, evidence_ref, application_id, excerpt, candidates)
            select $1,$2,$3,$4,$5,$6
             where not exists (
               select 1 from review_items
                where user_id = $1 and kind = $2 and evidence_ref = $3
                  and resolved_at is null)
            """,
            signal.user_id,
            kind,
            signal.evidence_ref,
            application_id,
            excerpt,
            # The column is `not null default '[]'`: a review item with nothing
            # to choose between still has an empty list of candidates, not a
            # missing one.
            candidates or [],
        )

    async def _mark_seen(
        self, connection: asyncpg.Connection, signal: Signal, outcome: Outcome
    ) -> None:
        """Through `loop.db.seen`, which is where the row count is read.

        Under row-level security an update whose predicate matches nothing is
        `UPDATE 0` and not an error, so a tenanting bug here and a working
        resolver are indistinguishable from the caller's side. This service had
        its own copy of the statement without that check — which is the one
        thing the shared helper exists for.
        """
        await mark_seen(connection, signal.mailbox_id, signal.provider_message_id, outcome)
