-- 005 · the queue and the scheduler
--
-- Both live in the same Postgres as the data they describe, which is the whole
-- reason the design has no Redis, no Kafka and no separate scheduler.
--
-- §02 names pgmq. This is the same surface — send, read with a visibility
-- timeout, delete, archive, metrics — implemented on one table with SKIP
-- LOCKED, because pgmq is not in the PGDG apt repository and building it drags
-- llvm-19-dev into an image meant for a free-tier ARM box. The spec's own
-- justification for pgmq ("transactional with the data it describes;
-- visibility timeouts are enough at this volume") holds identically here.
-- Swapping back is a search-and-replace of `mq.` for `pgmq.`. decisions.md B14.
--
-- §05 names the queues with dots (`raw.message`); the underscore forms below
-- are the same five, plus a dead-letter queue each.

create schema mq;

create table mq.messages (
  msg_id      bigserial primary key,
  queue       text not null,
  message     jsonb not null,
  read_ct     integer not null default 0,
  enqueued_at timestamptz not null default now(),
  -- Invisible until this instant. That is the whole visibility-timeout trick.
  vt          timestamptz not null default now()
);
-- The read path is (queue, vt) ordered by msg_id, so that is the index.
create index mq_messages_read_idx on mq.messages (queue, vt, msg_id);

create table mq.queues (
  name       text primary key,
  created_at timestamptz not null default now()
);

create function mq.create(p_queue text) returns void
language sql as $$
  insert into mq.queues (name) values (p_queue) on conflict do nothing;
$$;

create function mq.send(p_queue text, p_message jsonb, p_delay integer default 0)
returns bigint language sql as $$
  insert into mq.messages (queue, message, vt)
  values (p_queue, p_message, now() + make_interval(secs => p_delay))
  returning msg_id;
$$;

create function mq.send_batch(p_queue text, p_messages jsonb[])
returns setof bigint language sql as $$
  insert into mq.messages (queue, message)
  select p_queue, m from unnest(p_messages) as m
  returning msg_id;
$$;

-- Claim up to `qty` messages and hide them for `vt` seconds. SKIP LOCKED is
-- what lets several consumers of the same queue run without coordinating.
create function mq.read(p_queue text, p_vt integer, p_qty integer)
returns table (msg_id bigint, read_ct integer, enqueued_at timestamptz, vt timestamptz, message jsonb)
language sql as $$
  with claimed as (
    select m.msg_id from mq.messages m
    where m.queue = p_queue and m.vt <= now()
    order by m.msg_id
    limit p_qty
    for update skip locked
  )
  update mq.messages m
     set vt = now() + make_interval(secs => p_vt),
         read_ct = m.read_ct + 1
    from claimed c
   where m.msg_id = c.msg_id
  returning m.msg_id, m.read_ct, m.enqueued_at, m.vt, m.message;
$$;

create function mq.delete(p_queue text, p_msg_id bigint) returns boolean
language sql as $$
  with gone as (
    delete from mq.messages where queue = p_queue and msg_id = p_msg_id returning 1
  ) select count(*) > 0 from gone;
$$;

create function mq.metrics(p_queue text)
returns table (queue_name text, queue_length bigint, oldest_msg_age_sec integer, newest_msg_age_sec integer)
language sql stable as $$
  select p_queue,
         count(*),
         coalesce(extract(epoch from (now() - min(enqueued_at)))::integer, 0),
         coalesce(extract(epoch from (now() - max(enqueued_at)))::integer, 0)
  from mq.messages where queue = p_queue;
$$;

select mq.create('raw_message');        -- connector  → classifier
select mq.create('candidate_message');  -- classifier → extractor
select mq.create('signal_extracted');   -- extractor  → resolver
select mq.create('event_pending');      -- resolver   → pipeline
select mq.create('notify_pending');     -- nudge      → notifier

-- Dead letters get their own queue rather than an archive table, because an
-- archive keeps the payload verbatim and forever — and a payload contains
-- message text. The wrapper strips the body on the way in; what remains is
-- enough to replay from `seen_messages` once the bug is fixed. decisions.md C11.
select mq.create('raw_message_dlq');
select mq.create('candidate_message_dlq');
select mq.create('signal_extracted_dlq');
select mq.create('event_pending_dlq');
select mq.create('notify_pending_dlq');

create extension if not exists pg_cron;

-- ── the nightly dormancy sweep ─────────────────────────────────────────────
-- "03:00 user-local". pg_cron speaks one server clock, so the job runs every
-- hour and only acts on the users for whom it is currently 03:00 — which is
-- also what keeps it correct when a user changes their timezone setting.
create or replace function dormancy_due_users() returns setof uuid
language sql stable as $$
  select id from users where extract(hour from (now() at time zone tz)) = 3
$$;

-- The sweep only *enqueues*; the pipeline is still the single writer.
create or replace function sweep_dormancy() returns integer
language plpgsql as $$
declare n integer := 0; rec record;
begin
  for rec in
    select a.id, a.user_id, a.last_signal_at,
           greatest(coalesce(sd.stale_after_days, 21), coalesce(2 * d.p90_days, 0)) as threshold_days,
           case when d.n is not null then 'p90x2' else 'stale_after_days' end as threshold_used
    from applications a
    join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
    left join stage_dwell_in d
           on d.user_id = a.user_id and d.stage = a.current_stage and d.n >= 5
    where a.status = 'live'
      and a.merged_into_id is null
      and a.last_signal_at is not null
      and a.user_id in (select dormancy_due_users())
  loop
    if extract(epoch from (now() - rec.last_signal_at)) / 86400 > rec.threshold_days then
      perform mq.send('event_pending', jsonb_build_object(
        'user_id', rec.user_id,
        'application_id', rec.id,
        'event', jsonb_build_object(
          'type', 'went_silent',
          'occurred_at', now(),
          'confidence', 0.9,
          'payload', jsonb_build_object(
            'days_quiet', floor(extract(epoch from (now() - rec.last_signal_at)) / 86400),
            -- The spec's threshold is 2 × p90, but p90 needs five transitions
            -- to exist. Which rule actually applied is recorded, because the
            -- field was already in the payload for exactly this. decisions.md C10.
            'threshold_used', rec.threshold_used
          )
        )
      ));
      n := n + 1;
    end if;
  end loop;
  return n;
end $$;

-- ── the park drain ─────────────────────────────────────────────────────────
-- Nothing in the spec ever drained parked messages, yet failure state F4 tells
-- the user "the queue drains itself when the model comes back". decisions.md C9.
create or replace function drain_parked(max_attempts integer default 6)
returns integer language plpgsql as $$
declare n integer := 0; rec record;
begin
  for rec in
    select mailbox_id, provider_message_id, user_id
    from seen_messages
    where outcome = 'parked' and park_attempts < max_attempts
  loop
    perform mq.send('candidate_message', jsonb_build_object(
      'mailbox_id', rec.mailbox_id,
      'provider_message_id', rec.provider_message_id,
      'user_id', rec.user_id,
      'replay', true
    ));
    update seen_messages
       set outcome = null, park_attempts = park_attempts + 1
     where mailbox_id = rec.mailbox_id and provider_message_id = rec.provider_message_id;
    n := n + 1;
  end loop;

  -- Anything that exhausted its attempts becomes a question for the human
  -- rather than a silently dropped message.
  insert into review_items (user_id, kind, evidence_ref, excerpt)
  select user_id, 'low_confidence', provider_message_id,
         'The model was unreachable for this message. Answering it here teaches the rules either way.'
  from seen_messages
  where outcome = 'parked' and park_attempts >= max_attempts;

  update seen_messages set outcome = 'review'
   where outcome = 'parked' and park_attempts >= max_attempts;

  return n;
end $$;

-- ── interview_held ─────────────────────────────────────────────────────────
-- "Derived by cron 2h after ends_at with no cancellation."
create or replace function mark_interviews_held() returns integer
language plpgsql as $$
declare n integer := 0; rec record;
begin
  for rec in
    select i.id, i.user_id, i.application_id, i.ends_at, i.starts_at
    from interviews i
    where i.held is null and i.cancelled_at is null
      and coalesce(i.ends_at, i.starts_at + interval '1 hour') < now() - interval '2 hours'
  loop
    perform mq.send('event_pending', jsonb_build_object(
      'user_id', rec.user_id,
      'application_id', rec.application_id,
      'event', jsonb_build_object(
        'type', 'interview_held',
        'occurred_at', coalesce(rec.ends_at, rec.starts_at),
        'confidence', 0.95,
        'payload', jsonb_build_object('interview_id', rec.id)
      )
    ));
    n := n + 1;
  end loop;
  return n;
end $$;

-- Erasure, as one function, so the append-only escape hatch is never left on
-- by accident and the queue purge cannot be forgotten. "DELETE /api/account:
-- cascade + queue purge + vector index + push subscriptions."
create or replace function erase_user(p_user uuid) returns void
language plpgsql security definer as $$
begin
  perform set_config('loop.erasing', 'on', true);
  delete from mq.messages where message->>'user_id' = p_user::text;
  delete from users where id = p_user;
  perform set_config('loop.erasing', 'off', true);
end $$;

create or replace function nudge_tick() returns void
language sql as $$ select pg_notify('loop_nudge', 'tick') $$;

-- The queue is shared infrastructure: every service publishes to the next stage
-- and consumes from its own. There is nothing user-scoped in it — the payloads
-- carry a user_id that the consuming service uses to open a tenant session —
-- so it is granted flat rather than policed by RLS.
do $$
declare r text;
begin
  foreach r in array array[
    'loop_gateway','loop_connector','loop_classifier','loop_extractor',
    'loop_resolver','loop_pipeline','loop_nudge','loop_notifier'
  ] loop
    execute format('grant usage on schema mq to %I', r);
    execute format('grant select, insert, update, delete on mq.messages to %I', r);
    execute format('grant select on mq.queues to %I', r);
    execute format('grant usage, select on sequence mq.messages_msg_id_seq to %I', r);
    execute format('grant execute on all functions in schema mq to %I', r);
    execute format('grant execute on function erase_user(uuid) to %I', r);
  end loop;
end $$;

select cron.schedule('loop-dormancy',   '5 * * * *',    $$select sweep_dormancy()$$);
select cron.schedule('loop-park-drain', '*/15 * * * *', $$select drain_parked()$$);
select cron.schedule('loop-interviews', '*/30 * * * *', $$select mark_interviews_held()$$);
select cron.schedule('loop-nudge',      '*/15 * * * *', $$select nudge_tick()$$);
