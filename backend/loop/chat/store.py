"""Conversations and their messages, in Postgres, under the tenant session.

Two shapes leave this module. The wire shape is what the client renders:
messages with their tool traces and attachment ids. The model shape is what the
agent is given as history: OpenAI-format turns, user images inlined as data
URIs, and — deliberately — no tool payloads from previous turns. Email text
read by a tool exists only inside the turn that read it; if the model needs it
again, it reads it again, which costs one Gmail call.

That keeps a message body out of this table by construction on the one path
this module controls: no tool payload is ever written here. The other path is
the answer, which is the model's own words and is stored — so §04 holds across
the chat only as far as the rule in `prompt.py` that says to describe an email
and never transcribe it. Construction where it can be construction, an
instruction where it cannot: worth knowing which of the two you are relying on.
"""

import base64
from typing import Any, Final

from loop.db import Database
from loop.domain.clock import iso_z

# How much of a conversation the model sees. Beyond this the transcript is
# still on screen — it is only the context that forgets.
_MODEL_TURNS: Final = 24

# What the pictures in the window may weigh together when the history is
# rebuilt. Generous next to one screenshot, small next to twenty-four of them.
_MODEL_IMAGE_BYTES: Final = 8 * 1024 * 1024

# The first user message becomes the title, trimmed to a listing.
_TITLE_CHARS: Final = 80

_MAX_CONTENT_CHARS: Final = 8000


async def create_conversation(
    db: Database, user_id: str, *, model: str | None = None
) -> dict[str, Any]:
    async with db.session(user_id) as connection:
        row = await connection.fetchrow(
            """
            insert into chat_conversations (user_id, model)
            values ($1, $2) returning id, title, model, created_at, last_message_at
            """,
            user_id,
            model,
        )
    assert row is not None
    return _conversation(row, message_count=0)


async def list_conversations(db: Database, user_id: str) -> list[dict[str, Any]]:
    async with db.session(user_id) as connection:
        rows = await connection.fetch(
            """
            select c.id, c.title, c.model, c.created_at, c.last_message_at,
                   (select count(*) from chat_messages m where m.conversation_id = c.id)
                     as message_count
              from chat_conversations c
             where c.user_id = $1
             order by c.last_message_at desc
             limit 50
            """,
            user_id,
        )
    return [_conversation(row, message_count=int(row["message_count"])) for row in rows]


async def conversation_exists(db: Database, user_id: str, conversation_id: str) -> bool:
    async with db.session(user_id) as connection:
        return bool(
            await connection.fetchval(
                "select 1 from chat_conversations where id = $1 and user_id = $2",
                conversation_id,
                user_id,
            )
        )


async def delete_conversation(db: Database, user_id: str, conversation_id: str) -> bool:
    async with db.session(user_id) as connection:
        deleted = await connection.execute(
            "delete from chat_conversations where id = $1 and user_id = $2",
            conversation_id,
            user_id,
        )
    # asyncpg reports a delete as `DELETE <count>`.
    return bool(str(deleted).endswith(" 1"))


async def set_model(
    db: Database, user_id: str, conversation_id: str, model: str
) -> None:
    async with db.session(user_id) as connection:
        await connection.execute(
            "update chat_conversations set model = $3 where id = $1 and user_id = $2",
            conversation_id,
            user_id,
            model,
        )


async def messages_of(
    db: Database, user_id: str, conversation_id: str
) -> list[dict[str, Any]]:
    """The wire shape: everything the panel renders, oldest first."""
    async with db.session(user_id) as connection:
        rows = await connection.fetch(
            """
            select m.id, m.role, m.content, m.tool_trace, m.model, m.created_at,
                   coalesce(array_agg(a.id order by a.created_at)
                            filter (where a.id is not null), '{}') as attachment_ids
              from chat_messages m
              left join chat_attachments a on a.message_id = m.id
             where m.conversation_id = $1 and m.user_id = $2
             group by m.id
             order by m.created_at, m.id
            """,
            conversation_id,
            user_id,
        )
    return [
        {
            "id": str(row["id"]),
            "role": row["role"],
            "content": row["content"],
            "tool_trace": row["tool_trace"],
            "model": row["model"],
            "created_at": iso_z(row["created_at"]),
            "attachment_ids": [str(a) for a in row["attachment_ids"]],
        }
        for row in rows
    ]


async def append_message(
    db: Database,
    user_id: str,
    conversation_id: str,
    *,
    role: str,
    content: str,
    tool_trace: list[dict[str, Any]] | None = None,
    model: str | None = None,
    attachment_ids: list[str] | None = None,
) -> str:
    """One message, its attachments bound, the conversation stamped and titled."""
    async with db.session(user_id) as connection:
        message_id = str(
            await connection.fetchval(
                """
                insert into chat_messages
                  (conversation_id, user_id, role, content, tool_trace, model)
                values ($1,$2,$3,$4,$5,$6) returning id
                """,
                conversation_id,
                user_id,
                role,
                content[:_MAX_CONTENT_CHARS],
                tool_trace or [],
                model,
            )
        )
        if attachment_ids:
            await connection.execute(
                """
                update chat_attachments set message_id = $1
                 where id = any($2::uuid[]) and conversation_id = $3 and user_id = $4
                   and message_id is null
                """,
                message_id,
                attachment_ids,
                conversation_id,
                user_id,
            )
        await connection.execute(
            """
            update chat_conversations
               set last_message_at = now(),
                   title = coalesce(title, nullif(left($3, $4), ''))
             where id = $1 and user_id = $2
            """,
            conversation_id,
            user_id,
            content.strip().replace("\n", " ") if role == "user" else "",
            _TITLE_CHARS,
        )
    return message_id


async def drop_last_answer(
    db: Database, user_id: str, conversation_id: str
) -> bool:
    """Remove the answer at the end of the conversation, if there is one.

    What a retry is: the question stays, its answer goes, and the next stream
    writes the replacement. Only a trailing assistant message qualifies — a
    conversation whose last word is the user's has nothing to replace, and
    deleting further back would rewrite history rather than redo the last of it.
    """
    async with db.session(user_id) as connection:
        deleted = await connection.execute(
            """
            delete from chat_messages
             where id = (
               select id from chat_messages
                where conversation_id = $1 and user_id = $2
                order by created_at desc, id desc limit 1
             )
             and role = 'assistant'
            """,
            conversation_id,
            user_id,
        )
    return bool(str(deleted).endswith(" 1"))


async def add_attachment(
    db: Database,
    user_id: str,
    conversation_id: str,
    *,
    media_type: str,
    data: bytes,
) -> str:
    async with db.session(user_id) as connection:
        return str(
            await connection.fetchval(
                """
                insert into chat_attachments (user_id, conversation_id, media_type, bytes)
                values ($1,$2,$3,$4) returning id
                """,
                user_id,
                conversation_id,
                media_type,
                data,
            )
        )


async def unbound_attachments(
    db: Database, user_id: str, conversation_id: str
) -> int:
    """How many uploads are waiting for a message, which is what the cap reads."""
    async with db.session(user_id) as connection:
        return int(
            await connection.fetchval(
                """
                select count(*) from chat_attachments
                 where conversation_id = $1 and user_id = $2 and message_id is null
                """,
                conversation_id,
                user_id,
            )
            or 0
        )


async def attachment(
    db: Database, user_id: str, attachment_id: str
) -> tuple[str, bytes] | None:
    async with db.session(user_id) as connection:
        row = await connection.fetchrow(
            "select media_type, bytes from chat_attachments where id = $1 and user_id = $2",
            attachment_id,
            user_id,
        )
    return (row["media_type"], bytes(row["bytes"])) if row else None


async def model_history(
    db: Database, user_id: str, conversation_id: str
) -> list[dict[str, Any]]:
    """The model shape: recent turns, images inlined, tool payloads absent."""
    async with db.session(user_id) as connection:
        rows = await connection.fetch(
            """
            select m.id, m.role, m.content,
                   coalesce(array_agg(a.id order by a.created_at)
                            filter (where a.id is not null), '{}') as attachment_ids
              from chat_messages m
              left join chat_attachments a on a.message_id = m.id
             where m.conversation_id = $1 and m.user_id = $2
             group by m.id
             order by m.created_at desc, m.id desc
             limit $3
            """,
            conversation_id,
            user_id,
            _MODEL_TURNS,
        )
        # Sizes first, bytes second, and only for what fits. Every turn used to
        # re-read and re-inline every picture in the window, one query each —
        # so a conversation with a few screenshots in it re-sent tens of
        # megabytes on every question. The newest win: a picture from three
        # questions ago is context, and context is what a budget is for.
        wanted = [str(a) for row in rows for a in row["attachment_ids"]]
        attachments: dict[str, tuple[str, bytes]] = {}
        if wanted:
            sizes = {
                str(record["id"]): int(record["size"])
                for record in await connection.fetch(
                    "select id, octet_length(bytes) as size"
                    " from chat_attachments where id = any($1::uuid[])",
                    wanted,
                )
            }
            budget = _MODEL_IMAGE_BYTES
            keep: list[str] = []
            for row in rows:  # newest first, as fetched
                for attachment_id in row["attachment_ids"]:
                    size = sizes.get(str(attachment_id))
                    if size is None or size > budget:
                        continue
                    budget -= size
                    keep.append(str(attachment_id))
            for record in await connection.fetch(
                "select id, media_type, bytes from chat_attachments"
                " where id = any($1::uuid[])",
                keep,
            ):
                attachments[str(record["id"])] = (
                    record["media_type"],
                    bytes(record["bytes"]),
                )

    history: list[dict[str, Any]] = []
    for row in reversed(rows):
        history.append(
            _model_turn(
                role=row["role"],
                content=row["content"],
                images=[
                    attachments[str(a)]
                    for a in row["attachment_ids"]
                    if str(a) in attachments
                ],
            )
        )
    return history


def _model_turn(
    *, role: str, content: str, images: list[tuple[str, bytes]]
) -> dict[str, Any]:
    """One OpenAI-format turn; multimodal only when there is something to see."""
    if role != "user" or not images:
        return {"role": role, "content": content}
    parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
    for media_type, data in images:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": _data_uri(media_type, data)},
            }
        )
    return {"role": "user", "content": parts}


def _data_uri(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


def _conversation(row: Any, *, message_count: int) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "model": row["model"],
        "created_at": iso_z(row["created_at"]),
        "last_message_at": iso_z(row["last_message_at"]),
        "message_count": message_count,
    }
