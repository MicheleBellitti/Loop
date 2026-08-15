"""What the assistant may do, stated as data.

Every tool is a name, a description, a pydantic argument model and a handler —
the shape `langchain_core.tools.StructuredTool` consumes directly, kept
in-process because the gateway already holds the credentials and the tenant
session. A handler returns a `ToolResult`: `payload` is what the model reads
and lives only for the length of the turn; `summary` is one safe line, and it
is the only part that is ever persisted or shown in the interface.

Two disciplines, both inherited from the rest of the system:

**Email text never touches a table.** The read-email tool fetches from Gmail by
provider id — the same replay trick `seen_messages` exists for — hands the text
to the model fenced as untrusted data, and forgets it.

**No network inside a database session.** Each handler reads what it needs in a
short transaction, closes it, and only then talks to Google.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from loop.connector.normalise import to_raw_message
from loop.db import Database
from loop.domain import fence_message
from loop.domain.clock import iso_z
from loop.domain.thresholds import MAX_BACKFILL_MONTHS
from loop.google.client import GoogleAuthError, GoogleClient, GoogleRateLimit
from loop.google.mailbox import NoRefreshToken, read_refresh_token, to_mailbox

_log = logging.getLogger("loop.chat.tools")

# What one email contributes to the model's context. The connector already caps
# a body at 6000 characters; this is tighter because a chat turn may read
# several and the answer still has to fit.
_EMAIL_CHARS: Final = 4000

_MAX_LIST: Final = 50
_MAX_EMAILS_PER_CALL: Final = 3


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a handler is allowed to reach: this user's rows, and their Google."""

    db: Database
    user_id: str
    # None when no Google client is configured; the mail tools say so instead
    # of failing strangely.
    google: GoogleClient | None


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    # What the model reads. JSON-serialisable, discarded after the turn.
    payload: Any
    # One line, safe to persist and to print: never message text.
    summary: str


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


# ── applications ────────────────────────────────────────────────────────────

_APPLICATION_ROWS = """
select a.id, c.canonical_name as company, a.role_title, a.current_stage,
       a.current_phase, a.status, a.applied_at, a.last_signal_at,
       a.needs_review, a.confidence, s.channel
  from applications a
  join companies c on c.id = a.company_id
  left join lateral (
    select channel from sources
     where application_id = a.id order by is_first_touch desc limit 1
  ) s on true
 where a.user_id = $1 and a.merged_into_id is null
"""

_PHASES = frozenset({"sent", "screening", "interviewing", "decided"})
_STATUSES = frozenset({"live", "dormant", "rejected", "withdrawn", "accepted"})


async def _list_applications(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    conditions: list[str] = []
    params: list[object] = [context.user_id]
    phase, status, query = args.get("phase"), args.get("status"), args.get("query")
    if isinstance(phase, str) and phase in _PHASES:
        params.append(phase)
        conditions.append(f"and a.current_phase = ${len(params)}")
    if isinstance(status, str) and status in _STATUSES:
        params.append(status)
        conditions.append(f"and a.status = ${len(params)}")
    if isinstance(query, str) and query.strip():
        params.append(f"%{query.strip()}%")
        conditions.append(
            f"and (c.canonical_name ilike ${len(params)} or a.role_title ilike ${len(params)})"
        )
    limit = args.get("limit")
    capped = min(int(limit), _MAX_LIST) if isinstance(limit, int) and limit > 0 else 20
    params.append(capped)
    sql = (
        f"{_APPLICATION_ROWS} {' '.join(conditions)}"
        f" order by a.last_signal_at desc nulls last limit ${len(params)}"
    )

    async with context.db.session(context.user_id) as connection:
        rows = await connection.fetch(sql, *params)

    return ToolResult(
        ok=True,
        payload=[
            {
                "id": str(row["id"]),
                "company": row["company"],
                "role": row["role_title"],
                "stage": row["current_stage"],
                "phase": row["current_phase"],
                "status": row["status"],
                "channel": row["channel"],
                "applied_at": iso_z(row["applied_at"]),
                "last_signal_at": iso_z(row["last_signal_at"]),
                "needs_review": bool(row["needs_review"]),
            }
            for row in rows
        ],
        summary=f"{len(rows)} applications",
    )


_EVENTS = """
select type, occurred_at, to_stage, payload, confidence, rung, evidence_ref
  from application_events
 where application_id = $1 and user_id = $2
 order by occurred_at desc, id desc
 limit 40
"""


async def _get_application(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    application_id, failure = await _resolve_application(context, args)
    if application_id is None:
        return failure or _bad("no such application")

    async with context.db.session(context.user_id) as connection:
        row = await connection.fetchrow(
            f"{_APPLICATION_ROWS} and a.id = $2", context.user_id, application_id
        )
        if row is None:
            return _bad("no such application")
        events = await connection.fetch(_EVENTS, application_id, context.user_id)
        detail = await connection.fetchrow(
            """
            select location, work_mode, seniority, comp_expectation_minor, comp_currency
              from applications where id = $1 and user_id = $2
            """,
            application_id,
            context.user_id,
        )

    return ToolResult(
        ok=True,
        payload={
            "id": str(row["id"]),
            "company": row["company"],
            "role": row["role_title"],
            "stage": row["current_stage"],
            "phase": row["current_phase"],
            "status": row["status"],
            "channel": row["channel"],
            "applied_at": iso_z(row["applied_at"]),
            "last_signal_at": iso_z(row["last_signal_at"]),
            "location": detail["location"] if detail else None,
            "work_mode": detail["work_mode"] if detail else None,
            "seniority": detail["seniority"] if detail else None,
            "events": [
                {
                    "type": event["type"],
                    "occurred_at": iso_z(event["occurred_at"]),
                    "to_stage": event["to_stage"],
                    "confidence": float(event["confidence"]),
                    "rung": event["rung"],
                    # The provider message id — feed it to read_application_email
                    # to see the message behind the claim.
                    "evidence_ref": event["evidence_ref"],
                }
                for event in events
            ],
        },
        summary=f"{row['company']} · {row['role_title']} · {len(events)} events",
    )


# ── statistics ──────────────────────────────────────────────────────────────


async def _get_statistics(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    # Imported here rather than at module scope: `loop.api` imports this module
    # through its chat routes, and a top-level import back into `loop.api`
    # would be a cycle. By the time a tool runs, both are long since loaded.
    from loop.api.routes.stats import stats_payload

    period = args.get("period")
    chosen = period if isinstance(period, str) else "12m"
    async with context.db.session(context.user_id) as connection:
        payload = await stats_payload(connection, context.user_id, chosen)
    return ToolResult(ok=True, payload=payload, summary=f"statistics over {payload['period']}")


# ── the mailbox ─────────────────────────────────────────────────────────────

_EVIDENCE = """
select e.evidence_ref, e.type, e.occurred_at, s.mailbox_id, s.outcome, s.received_at
  from application_events e
  join seen_messages s
    on s.provider_message_id = e.evidence_ref and s.user_id = e.user_id
 where e.application_id = $1 and e.user_id = $2 and e.evidence_ref is not null
 order by e.occurred_at desc, e.id desc
"""

_MAILBOX = """
select id, user_id, provider, address, secret_ciphertext, secret_nonce,
       dek_wrapped, dek_nonce, scopes, cursor, watch_expires_at, status, last_ok_at
  from mailbox_accounts where id = $1 and user_id = $2
"""


async def _list_application_emails(
    context: ToolContext, args: dict[str, Any]
) -> ToolResult:
    application_id, failure = await _resolve_application(context, args)
    if application_id is None:
        return failure or _bad("no such application")

    async with context.db.session(context.user_id) as connection:
        rows = await connection.fetch(_EVIDENCE, application_id, context.user_id)

    seen: set[str] = set()
    emails: list[dict[str, Any]] = []
    for row in rows:
        if row["evidence_ref"] in seen:
            continue
        seen.add(row["evidence_ref"])
        emails.append(
            {
                "provider_message_id": row["evidence_ref"],
                "produced_event": row["type"],
                "occurred_at": iso_z(row["occurred_at"]),
                "received_at": iso_z(row["received_at"]),
            }
        )
    return ToolResult(ok=True, payload=emails, summary=f"{len(emails)} emails on record")


async def _read_application_email(
    context: ToolContext, args: dict[str, Any]
) -> ToolResult:
    application_id, failure = await _resolve_application(context, args)
    if application_id is None:
        return failure or _bad("no such application")
    if context.google is None:
        return _bad("no Google client is configured, so the mailbox cannot be read")

    wanted = args.get("provider_message_id")
    limit = args.get("limit")
    count = (
        min(int(limit), _MAX_EMAILS_PER_CALL)
        if isinstance(limit, int) and limit > 0
        else 1
    )

    # Everything the database knows, in one short transaction — then the
    # connection goes back before Gmail is asked anything.
    async with context.db.session(context.user_id) as connection:
        rows = await connection.fetch(_EVIDENCE, application_id, context.user_id)
        chosen: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            ref = row["evidence_ref"]
            if ref in seen:
                continue
            if isinstance(wanted, str) and wanted and ref != wanted:
                continue
            seen.add(ref)
            chosen.append((ref, str(row["mailbox_id"])))
            if len(chosen) >= (1 if wanted else count):
                break
        mailboxes = {
            mailbox_id: to_mailbox(record)
            for mailbox_id in {m for _, m in chosen}
            if (
                record := await connection.fetchrow(
                    _MAILBOX, mailbox_id, context.user_id
                )
            )
            is not None
        }

    if not chosen:
        return _bad(
            "no email is on record for that application"
            if not wanted
            else "that message is not on record for that application"
        )

    read: list[dict[str, Any]] = []
    for provider_message_id, mailbox_id in chosen:
        mailbox = mailboxes.get(mailbox_id)
        if mailbox is None:
            continue
        try:
            token = await context.google.access_token(
                mailbox.id, read_refresh_token(mailbox)
            )
            message = await context.google.hydrate_calendar_parts(
                token, await context.google.get_message(token, provider_message_id)
            )
        except NoRefreshToken:
            return _bad("the mailbox has no refresh token; reconnect it in settings")
        except GoogleAuthError:
            return _bad("the mailbox needs to be reconnected before mail can be read")
        except GoogleRateLimit:
            return _bad("Google is rate-limiting; try again in a little while")
        except Exception:
            _log.exception("could not read %s", provider_message_id)
            return _bad("that message could not be fetched from Gmail")

        raw = to_raw_message(message, user_id=context.user_id, mailbox_id=mailbox.id)
        read.append(
            {
                "provider_message_id": provider_message_id,
                "from": raw.headers.sender,
                "subject": raw.headers.subject,
                "received_at": iso_z(raw.received_at),
                # Fenced: a recruiter's signature is a real injection vector,
                # and the fence marks everything inside as data, not orders.
                "body": fence_message(raw.text[:_EMAIL_CHARS]),
            }
        )

    if not read:
        return _bad("the mailbox holding that message is no longer connected")
    return ToolResult(
        ok=True,
        payload=read,
        summary=f"read {len(read)} email{'s' if len(read) != 1 else ''} from Gmail",
    )


async def _get_mailbox_health(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    from loop.api.mailbox import mailbox_health  # late: see _get_statistics

    async with context.db.session(context.user_id) as connection:
        payload = await mailbox_health(connection, context.user_id)
    return ToolResult(ok=True, payload=payload, summary=f"mailbox state {payload['state']}")


async def _start_backfill(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    months = args.get("months")
    if not isinstance(months, int) or not 1 <= months <= MAX_BACKFILL_MONTHS:
        return _bad(f"months must be between 1 and {MAX_BACKFILL_MONTHS}")

    async with context.db.session(context.user_id) as connection:
        mailbox_id = await connection.fetchval(
            "select id from mailbox_accounts where user_id = $1 order by created_at limit 1",
            context.user_id,
        )
    if mailbox_id is None:
        return _bad("no mailbox is connected")

    # The same notification the backfill button sends: the connector holds the
    # credentials and the rate budget, the chat only says when.
    async with context.db.untenanted() as connection:
        await connection.execute(
            "select pg_notify('loop_backfill', $1)",
            json.dumps({"mailbox_id": str(mailbox_id), "months": months}),
        )
    return ToolResult(
        ok=True,
        payload={"ok": True, "months": months},
        summary=f"backfill of {months} months requested",
    )


# ── plumbing ────────────────────────────────────────────────────────────────


def _bad(reason: str, payload: Any = None) -> ToolResult:
    return ToolResult(
        ok=False, payload={"error": reason} if payload is None else payload, summary=reason
    )


_MATCHES = """
select a.id, c.canonical_name as company, a.role_title, a.current_stage, a.status
  from applications a
  join companies c on c.id = a.company_id
 where a.user_id = $1 and a.merged_into_id is null
   and (c.canonical_name ilike $2 or a.role_title ilike $2)
 order by (lower(c.canonical_name) = lower($3)) desc,
          a.last_signal_at desc nulls last
 limit 6
"""


async def _resolve_application(
    context: ToolContext, args: dict[str, Any]
) -> tuple[str | None, ToolResult | None]:
    """Which application the caller meant, by id or by name.

    A seven-billion-parameter model does not carry a UUID across three turns
    intact, and asking it to is how this tool failed in the field: it invented
    plausible ids. So the argument is whatever the user would say — "Prima",
    "the Staff Engineer one" — and the lookup happens here, where the rows are.
    An id still works and still wins, because the panel sends one.

    Returns the id, or a failure the model can act on: no match names the
    fallback, several matches list themselves so the next call can be exact.
    """
    raw = args.get("application") or args.get("application_id")
    if not isinstance(raw, str) or not raw.strip():
        return None, _bad("name the application: its company, its role, or its id")
    value = raw.strip()
    if _UUID.match(value):
        return value, None

    async with context.db.session(context.user_id) as connection:
        rows = await connection.fetch(_MATCHES, context.user_id, f"%{value}%", value)

    if not rows:
        return None, _bad(
            f"nothing matches {value!r}; call list_applications to see what there is"
        )
    exact = [row for row in rows if row["company"].lower() == value.lower()]
    if len(rows) == 1 or len(exact) == 1:
        return str((exact or rows)[0]["id"]), None
    return None, _bad(
        f"{len(rows)} applications match {value!r}; ask again with one of these ids",
        {
            "candidates": [
                {
                    "id": str(row["id"]),
                    "company": row["company"],
                    "role": row["role_title"],
                    "stage": row["current_stage"],
                    "status": row["status"],
                }
                for row in rows
            ]
        },
    )


# The same shape the applications routes accept: dashed lowercase-hex, exactly.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ── argument models ─────────────────────────────────────────────────────────
# Pydantic, because that is what StructuredTool consumes and what llama.cpp is
# sent as the function schema. The handlers above still validate defensively —
# a schema is a request, the handler is the guarantee, and that only holds
# while the schema stays a request: a bound LangChain enforces is one the
# handler never gets to clamp, so a model asking for 100 rows or saying
# "applied" gets nothing back instead of the fifty rows it meant.

_PHASE_VALUES = ", ".join(sorted(_PHASES))
_STATUS_VALUES = ", ".join(sorted(_STATUSES))


class ListApplicationsArgs(BaseModel):
    phase: str | None = Field(default=None, description=f"one of: {_PHASE_VALUES}")
    status: str | None = Field(default=None, description=f"one of: {_STATUS_VALUES}")
    query: str | None = Field(
        default=None, description="matches the company or the role title"
    )
    limit: int | None = Field(
        default=None, description=f"at most {_MAX_LIST}; larger asks are capped"
    )


_WHICH: Final = (
    'which application: the company ("Prima"), the role title, or the UUID if '
    "you have one"
)


class ApplicationArgs(BaseModel):
    application: str | None = Field(default=None, description=_WHICH)
    # The same thing under the name a model reaches for once it has seen an id.
    # Accepting both spellings costs a line here and saves a wasted round.
    application_id: str | None = Field(default=None, description=_WHICH)


class StatisticsArgs(BaseModel):
    period: Literal["90d", "12m", "all"] | None = None


class ReadEmailArgs(BaseModel):
    application: str | None = Field(default=None, description=_WHICH)
    application_id: str | None = Field(default=None, description=_WHICH)
    provider_message_id: str | None = Field(
        default=None,
        description=(
            "a message id from list_application_emails or an event's evidence_ref; "
            "omit to read the most recent"
        ),
    )
    limit: int | None = Field(
        default=None,
        description=(
            "how many recent emails to read when no id is given; "
            f"at most {_MAX_EMAILS_PER_CALL}, larger asks are capped"
        ),
    )


class NoArgs(BaseModel):
    pass


class BackfillArgs(BaseModel):
    months: int = Field(description=f"between 1 and {MAX_BACKFILL_MONTHS}")


def default_tools() -> tuple[Tool, ...]:
    """The registry, in the order the system prompt lists them."""
    return (
        Tool(
            name="list_applications",
            description=(
                "List the user's job applications: company, role, stage, status and "
                "when each last moved. Filter by phase, status or a text query."
            ),
            args=ListApplicationsArgs,
            run=_list_applications,
        ),
        Tool(
            name="get_application",
            description=(
                "One application in full: its facts and its whole event log, each "
                "event with its confidence and the id of the email it came from. "
                "Name it however the user did — the company is enough."
            ),
            args=ApplicationArgs,
            run=_get_application,
        ),
        Tool(
            name="get_statistics",
            description=(
                "The pipeline statistics: funnel, conversion ratios with their "
                "numerators and denominators, response times, ghost rate, channel "
                "effectiveness, time in stage and compensation."
            ),
            args=StatisticsArgs,
            run=_get_statistics,
        ),
        Tool(
            name="list_application_emails",
            description=(
                "The emails on record for one application, named the same way: "
                "provider message ids and the event each produced. No message "
                "text — use read_application_email for that."
            ),
            args=ApplicationArgs,
            run=_list_application_emails,
        ),
        Tool(
            name="read_application_email",
            description=(
                "Fetch and read the text of an email behind an application, live "
                "from the mailbox. Reads the most recent one unless a "
                "provider_message_id from list_application_emails or an event's "
                "evidence_ref is given."
            ),
            args=ReadEmailArgs,
            run=_read_application_email,
        ),
        Tool(
            name="get_mailbox_health",
            description=(
                "Whether the mailbox is connected, when it was last read, and "
                "the backlog."
            ),
            args=NoArgs,
            run=_get_mailbox_health,
        ),
        Tool(
            name="start_backfill",
            description=(
                "Ask the connector to re-read the mailbox this many months back. "
                "Only do this when the user explicitly asks for a backfill or rescan."
            ),
            args=BackfillArgs,
            run=_start_backfill,
        ),
    )


# What a tool result may weigh in the model's context. Payloads are for the
# model, but a context is finite and one exuberant tool must not evict the
# conversation.
_MAX_RESULT_CHARS: Final = 24_000


def rendered(result: ToolResult) -> str:
    """The string the model reads back, `summary` included so the stream layer
    can lift it from the ToolMessage without a side channel."""
    envelope = {"ok": result.ok, "summary": result.summary, "result": result.payload}
    text = json.dumps(envelope, ensure_ascii=False)
    if len(text) > _MAX_RESULT_CHARS:
        # The payload is what gets cut, never the envelope around it. Slicing
        # the finished document leaves JSON nothing can parse, and `ok` and
        # `summary` are read back out of it — a half-written envelope reports
        # the wrong outcome and puts raw payload, email text included, in the
        # one field that is persisted.
        envelope["result"] = {
            "truncated": True,
            "note": "too large to return in full — ask for less at a time",
            "head": json.dumps(result.payload, ensure_ascii=False)[
                : _MAX_RESULT_CHARS // 2
            ],
        }
        text = json.dumps(envelope, ensure_ascii=False)
    return text


def langchain_tools(
    context: ToolContext, tools: tuple[Tool, ...] | None = None
) -> list[StructuredTool]:
    """The registry, bound to one request's context, as LangChain tools.

    The wrapper never raises: whatever a handler does wrong becomes a result
    the model reads and explains, not a 500 in the stream.
    """

    def bound(tool: Tool) -> StructuredTool:
        async def call(**arguments: Any) -> str:
            try:
                result = await tool.run(context, arguments)
            except Exception:
                _log.exception("tool %s failed", tool.name)
                result = ToolResult(
                    ok=False,
                    payload={"error": "the tool failed; try something else"},
                    summary=f"{tool.name} failed",
                )
            return rendered(result)

        return StructuredTool.from_function(
            coroutine=call,
            name=tool.name,
            description=tool.description,
            args_schema=tool.args,
        )

    return [bound(tool) for tool in (tools if tools is not None else default_tools())]
