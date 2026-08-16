-- 020 · the chat surface
--
-- Conversations with the assistant that reads this pipeline. Three tables, all
-- user-scoped, all disposable: nothing downstream derives from them and
-- deleting a conversation deletes everything it carried.
--
-- §04's rule — no table ever stores message bodies — holds here on the path
-- this schema controls. A chat tool that reads an email fetches it from the
-- provider by id, the text lives in the model's context for the length of one
-- turn, and what persists is `tool_trace`: the tool's name, its arguments and
-- a one-line outcome, never its output.
--
-- `messages.content` is the exception to know about. It is the model's answer,
-- not a message, but an answer that transcribed an email would carry one in
-- here — so the system prompt forbids transcribing, and that instruction is
-- what the rule rests on for this column. Nothing in the schema can enforce it.

create table chat_conversations (
  id              uuid primary key default uuid_generate_v7(),
  user_id         uuid not null references users on delete cascade,
  title           text,
  -- The model the user picked for this thread, or null for the default. Free
  -- text on purpose: the list of models is whatever the llama.cpp server
  -- reports today, and a check constraint would freeze it.
  model           text,
  created_at      timestamptz not null default now(),
  last_message_at timestamptz not null default now()
);
create index chat_conversations_user_idx
  on chat_conversations (user_id, last_message_at desc);

create table chat_messages (
  id              uuid primary key default uuid_generate_v7(),
  conversation_id uuid not null references chat_conversations on delete cascade,
  user_id         uuid not null references users on delete cascade,
  role            text not null check (role in ('user','assistant')),
  content         text not null,
  -- What the assistant did on the way to `content`. Summaries only — see the
  -- header. `[]` for user messages.
  tool_trace      jsonb not null default '[]',
  model           text,
  created_at      timestamptz not null default now()
);
create index chat_messages_conversation_idx
  on chat_messages (conversation_id, created_at);

-- Images the user attaches. Stored in the row rather than on disk because they
-- are small (the gateway caps them), tenant-scoped by the same policy as
-- everything else, and erased by the same cascade.
create table chat_attachments (
  id              uuid primary key default uuid_generate_v7(),
  user_id         uuid not null references users on delete cascade,
  conversation_id uuid not null references chat_conversations on delete cascade,
  -- Null until the message it belongs to is sent; bound at send time so an
  -- upload the user abandons is identifiable and reapable.
  message_id      uuid references chat_messages on delete cascade,
  media_type      text not null check (media_type in
                    ('image/png','image/jpeg','image/webp','image/gif')),
  bytes           bytea not null,
  created_at      timestamptz not null default now()
);
create index chat_attachments_conversation_idx
  on chat_attachments (conversation_id);

-- Row-level security, the same shape as 003: enabled and forced from the first
-- migration that creates the table, so multi-tenancy stays a deployment flag.
do $$
declare t text;
begin
  foreach t in array array['chat_conversations','chat_messages','chat_attachments'] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format(
      'create policy tenant on %I using (user_id = loop_current_user())
                                with check (user_id = loop_current_user())', t);
  end loop;
end $$;

-- SELECT arrived through 003's default privileges; the writes are the
-- gateway's, because the chat is served by the gateway and nothing else
-- touches these tables.
grant insert, update, delete
  on chat_conversations, chat_messages, chat_attachments
  to loop_gateway;
