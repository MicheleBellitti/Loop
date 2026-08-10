-- 003 · row-level security and the per-service grants
--
-- RLS is enabled on every user-scoped table from the first migration, not
-- later, so that "open it up to a second person" stays a deployment flag rather
-- than a rewrite.
--
-- Two corrections to §04's one-line policy (decisions.md B5):
--   · `current_setting('loop.user_id')` throws whenever the GUC is unset — in a
--     migration, a maintenance session, a cron job. The two-argument form
--     returns null instead, and a null tenant sees nothing.
--   · `enable row level security` exempts the table owner. Since the owner is
--     the role migrations run as, and services would otherwise be tempted to
--     reuse it, every table also gets `force`.

create or replace function loop_current_user() returns uuid
language sql stable as $$
  select nullif(current_setting('loop.user_id', true), '')::uuid
$$;

do $$
declare t text;
begin
  foreach t in array array[
    'applications','application_events','sources','mailbox_accounts','seen_messages',
    'interviews','comp_offers','deadlines','review_items','suggestions',
    'push_subscriptions','notifications_sent','stage_defs','company_aliases',
    'credentials','auth_secrets','sessions','consents'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format(
      'create policy tenant on %I using (user_id = loop_current_user())
                                with check (user_id = loop_current_user())', t);
  end loop;
end $$;

-- `users` is scoped on its own primary key.
alter table users enable row level security;
alter table users force row level security;
create policy tenant on users
  using (id = loop_current_user()) with check (id = loop_current_user());

-- Companies are global reference data: readable by everyone, writable by the
-- resolver only. No policy, no user_id — see decisions.md B7 for why aliases
-- are the part that had to become user-scoped.

-- ── Per-service roles ──────────────────────────────────────────────────────
-- "Only the pipeline service writes to application_events, applications,
-- sources. Enforced by database grants, not by convention." (Spec §04)

do $$
declare r text;
begin
  foreach r in array array[
    'loop_gateway','loop_connector','loop_classifier','loop_extractor',
    'loop_resolver','loop_pipeline','loop_nudge','loop_notifier'
  ] loop
    if not exists (select 1 from pg_roles where rolname = r) then
      execute format('create role %I login', r);
    end if;
    execute format('grant usage on schema public to %I', r);
    execute format('grant select on all tables in schema public to %I', r);
    execute format('grant usage, select on all sequences in schema public to %I', r);
    execute format('alter default privileges in schema public grant select on tables to %I', r);
  end loop;
end $$;

-- The single writer of application state.
grant insert on application_events to loop_pipeline;
grant insert, update on applications to loop_pipeline;
grant insert, update, delete on sources to loop_pipeline;
grant insert, update, delete on interviews, comp_offers, deadlines to loop_pipeline;

-- Everyone else gets exactly what they need and nothing adjacent.
grant insert, update on seen_messages to loop_connector;
grant update on mailbox_accounts to loop_connector;
grant insert on applications to loop_connector;          -- never: revoked below
revoke insert on applications from loop_connector;

grant update on seen_messages to loop_classifier, loop_extractor, loop_resolver;
grant insert, update, delete on review_items to loop_resolver;
grant insert, update on companies, company_aliases to loop_resolver;
grant insert on applications to loop_resolver;           -- resolver creates the row…
grant update on applications to loop_resolver;           -- …and links merges

grant insert, update, delete on suggestions to loop_nudge;
grant insert on notifications_sent to loop_notifier;
grant delete on push_subscriptions to loop_notifier;

grant insert, update, delete on
  sessions, credentials, auth_secrets, push_subscriptions, consents,
  stage_defs, review_items, suggestions
  to loop_gateway;
grant insert, update on users, mailbox_accounts to loop_gateway;
grant insert on application_events to loop_gateway;
revoke insert on application_events from loop_gateway;   -- corrections go via the queue

-- The append-only guarantee, held by the database rather than by discipline.
--
-- With one deliberate hole: Article 17 erasure is a cascade, and a cascade is a
-- DELETE. Without an escape hatch `delete from users` fails and the product
-- cannot honour a deletion request at all — so the hatch exists, it is a
-- session flag nothing but the erasure path sets, and it is named after what it
-- is for rather than being a generic bypass.
create or replace function forbid_mutation() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' and coalesce(current_setting('loop.erasing', true), '') = 'on' then
    return old;
  end if;
  raise exception
    'application_events is append-only; a correction is a new human_corrected event'
    using errcode = 'restrict_violation';
end $$;

create trigger application_events_no_update
  before update or delete on application_events
  for each row execute function forbid_mutation();


