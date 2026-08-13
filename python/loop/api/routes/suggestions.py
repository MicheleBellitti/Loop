"""Three cards at most, and three things you can do with one.

Act, dismiss, later. None of them performs the suggestion — acting on a
follow-up means opening the draft and sending it yourself — so all three are one
timestamp, and which timestamp decides whether the card comes back.

`later` is the interesting one. It sets `snoozed_until`, which the read below
filters on and the nudge service's re-evaluation deliberately does not: the card
hides for a day while the rule stays satisfied, so it returns without a second
notification. Act and dismiss are permanent, per key, forever.
"""

from typing import Any, Final

from fastapi import APIRouter, Request

from loop.api import auth, narrate
from loop.api.drafts import Language, build_draft
from loop.api.errors import ApiError
from loop.api.serialise import iso_z
from loop.db import load_stage_table

router = APIRouter(prefix="/api")

# The display budget, and the same number the nudge rules rank down to.
_MOST_CARDS: Final = 3

_ACTIONS = {"act": "acted_at", "dismiss": "dismissed_at"}
_SNOOZE_INTERVAL = "1 day"

_OPEN = """
select key, rule, application_ids, payload, expires_at from suggestions
 where user_id = $1 and acted_at is null and dismissed_at is null
   and (snoozed_until is null or snoozed_until < now())
   and (expires_at is null or expires_at > now())
 order by created_at desc, key limit 3
"""


@router.get("/suggestions")
async def list_suggestions(request: Request) -> dict[str, Any]:
    """A different shape from the same rows on `/api/today`, inherited.

    There the payload is spread to the top level and this route's
    `application_ids` and `expires_at` are absent; here the payload is nested.
    Two renderers for one row, and not worth reconciling while both
    implementations share a database.
    """
    session = auth.require(getattr(request.state, "session", None))
    async with request.app.state.db.session(session.user_id) as connection:
        rows = await connection.fetch(_OPEN, session.user_id)
    return {
        "suggestions": [
            {
                "key": row["key"],
                "rule": row["rule"],
                "application_ids": [str(a) for a in row["application_ids"]],
                "payload": row["payload"] or {},
                "expires_at": iso_z(row["expires_at"]),
            }
            for row in rows
        ]
    }


@router.post("/suggestions/{key}/{action}")
async def act_on(request: Request, key: str, action: str) -> dict[str, Any]:
    session = auth.require(getattr(request.state, "session", None))
    if action == "snooze":
        return await _snooze(request, session.user_id, key)
    column = _ACTIONS.get(action)
    if column is None:
        raise ApiError(404, "not_found", "no such action")

    async with request.app.state.db.session(session.user_id) as connection:
        # The column name is interpolated from a fixed mapping, never from the
        # path; `key` is bound.
        await connection.execute(
            f"update suggestions set {column} = now() where user_id = $1 and key = $2",
            session.user_id,
            key,
        )
    # No row count check, and none in the reference: a key that matches nothing
    # is a card someone else already dealt with, not an error worth a dialog.
    return {"ok": True}


async def _snooze(request: Request, user_id: str, key: str) -> dict[str, Any]:
    async with request.app.state.db.session(user_id) as connection:
        await connection.execute(
            f"""
            update suggestions set snoozed_until = now() + interval '{_SNOOZE_INTERVAL}'
             where user_id = $1 and key = $2
            """,
            user_id,
            key,
        )
    return {"ok": True}


@router.get("/suggestions/{key}/draft")
async def draft(request: Request, key: str) -> dict[str, Any]:
    """The message, for you to send. Never for this to send.

    Deliberately unfiltered on state, as in the reference: a draft is served for
    a suggestion you already acted on, because "let me see what I sent" is a
    reasonable thing to ask and the card's status has nothing to do with it.
    """
    session = auth.require(getattr(request.state, "session", None))
    async with request.app.state.db.session(session.user_id) as connection:
        application_ids = await connection.fetchval(
            "select application_ids from suggestions where user_id = $1 and key = $2",
            session.user_id,
            key,
        )
        # Only the first: a batched suggestion drafts for one application and
        # says nothing about the others, which is a real limitation of the
        # let-it-go card and not something a draft can paper over.
        if not application_ids:
            raise ApiError(404, "not_found", "no draft for that suggestion")

        application = await connection.fetchrow(
            """
            select c.canonical_name as company, a.current_stage
              from applications a join companies c on c.id = a.company_id
             where a.id = $1 and a.merged_into_id is null
            """,
            str(application_ids[0]),
        )
        if application is None:
            raise ApiError(404, "not_found", "no draft for that suggestion")
        stages = await load_stage_table(connection, session.user_id)
        last = await connection.fetchrow(
            """
            select type, to_stage, payload from application_events
             where application_id = $1 order by occurred_at desc, id desc limit 1
            """,
            str(application_ids[0]),
        )

    payload = (last["payload"] or {}) if last else {}
    composed = build_draft(
        company=application["company"],
        language=_language(payload.get("language")),
        # The reference passed the *current stage's* label here, so a draft read
        # "thanks again for the hr call" whatever had actually happened last —
        # it selected the event's type and then used neither. This is the last
        # event, which is what the sentence claims to be about.
        last_event=narrate.title(last["type"], last["to_stage"], stages) if last else None,
        thread_id=payload.get("thread_id"),
    )
    return {
        "subject": composed.subject,
        "body": composed.body,
        "mailto_url": composed.mailto_url,
        "can_send": composed.can_send,
        "note": composed.note,
    }


def _language(value: Any) -> Language:
    """Anything that is not Italian is written in English.

    The payload is arbitrary jsonb, so this is a check rather than a cast — and
    note that the language comes from the most recent event alone, which means a
    `went_silent` written by the sweep resets an Italian thread to English.
    """
    return "it" if value == "it" else "en"
