"""What the assistant may do, stated as data.

Every tool is a name, a description, a JSON schema and a handler — the same
contract an MCP server would publish, kept in-process because the gateway
already holds the credentials and the tenant session. A handler returns a
`ToolResult`: `payload` is what the model reads and lives only for the length
of the turn; `summary` is one safe line, and it is the only part that is ever
persisted or shown in the interface.

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
from typing import Any, Final

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
    parameters: dict[str, Any]
    run: Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]

    def wire(self) -> dict[str, Any]:
        """The OpenAI `tools` entry llama.cpp expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


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
    application_id = _uuid_arg(args, "application_id")
    if application_id is None:
        return _bad("application_id must be a UUID")

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
    application_id = _uuid_arg(args, "application_id")
    if application_id is None:
        return _bad("application_id must be a UUID")

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
    application_id = _uuid_arg(args, "application_id")
    if application_id is None:
        return _bad("application_id must be a UUID")
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


def _bad(reason: str) -> ToolResult:
    return ToolResult(ok=False, payload={"error": reason}, summary=reason)


# The same shape the applications routes accept: dashed lowercase-hex, exactly.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _uuid_arg(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if not isinstance(value, str):
        return None
    return value if _UUID.match(value) else None


def default_tools() -> tuple[Tool, ...]:
    """The registry, in the order the system prompt lists them."""
    return (
        Tool(
            name="list_applications",
            description=(
                "List the user's job applications: company, role, stage, status and "
                "when each last moved. Filter by phase, status or a text query."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "phase": {"type": "string", "enum": sorted(_PHASES)},
                    "status": {"type": "string", "enum": sorted(_STATUSES)},
                    "query": {
                        "type": "string",
                        "description": "matches the company or the role title",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST},
                },
            },
            run=_list_applications,
        ),
        Tool(
            name="get_application",
            description=(
                "One application in full: its facts and its whole event log, each "
                "event with its confidence and the id of the email it came from."
            ),
            parameters={
                "type": "object",
                "properties": {"application_id": {"type": "string"}},
                "required": ["application_id"],
            },
            run=_get_application,
        ),
        Tool(
            name="get_statistics",
            description=(
                "The pipeline statistics: funnel, conversion ratios with their "
                "numerators and denominators, response times, ghost rate, channel "
                "effectiveness, time in stage and compensation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "period": {"type": "string", "enum": ["90d", "12m", "all"]}
                },
            },
            run=_get_statistics,
        ),
        Tool(
            name="list_application_emails",
            description=(
                "The emails on record for one application: provider message ids and "
                "the event each produced. No message text — use "
                "read_application_email for that."
            ),
            parameters={
                "type": "object",
                "properties": {"application_id": {"type": "string"}},
                "required": ["application_id"],
            },
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
            parameters={
                "type": "object",
                "properties": {
                    "application_id": {"type": "string"},
                    "provider_message_id": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_EMAILS_PER_CALL,
                        "description": "how many recent emails to read when no id is given",
                    },
                },
                "required": ["application_id"],
            },
            run=_read_application_email,
        ),
        Tool(
            name="get_mailbox_health",
            description=(
                "Whether the mailbox is connected, when it was last read, and "
                "the backlog."
            ),
            parameters={"type": "object", "properties": {}},
            run=_get_mailbox_health,
        ),
        Tool(
            name="start_backfill",
            description=(
                "Ask the connector to re-read the mailbox this many months back. "
                "Only do this when the user explicitly asks for a backfill or rescan."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_BACKFILL_MONTHS,
                    }
                },
                "required": ["months"],
            },
            run=_start_backfill,
        ),
    )
