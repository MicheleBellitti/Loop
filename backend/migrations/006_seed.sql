-- 006 · per-user seeding and the retention sweep

-- The default stage set. `draft` from §10 is absent: no event in the catalogue
-- could produce it, so it was unreachable, and depth 0 is left free in case a
-- future `saved` event brings it back. decisions.md A7.
create or replace function seed_stage_defs(p_user uuid) returns void
language sql as $$
  insert into stage_defs (user_id, key, label, phase, depth, stale_after_days)
  values
    (p_user, 'applied',            'Applied',             'sent',          1,  21),
    (p_user, 'acknowledged',       'Acknowledged',        'sent',          2,  21),
    (p_user, 'recruiter_reachout', 'Recruiter reach-out', 'screening',     4,  10),
    (p_user, 'hr_call',            'HR call',             'screening',     5,  10),
    (p_user, 'take_home',          'Take-home',           'screening',     6,  14),
    (p_user, 'technical',          'Technical',           'interviewing',  7,  12),
    (p_user, 'system_design',      'System design',       'interviewing',  8,  12),
    (p_user, 'onsite_loop',        'Onsite loop',         'interviewing',  9,  14),
    (p_user, 'final',              'Final',               'interviewing', 10,  10),
    (p_user, 'offer',              'Offer',               'decided',      12,   7),
    (p_user, 'negotiating',        'Negotiating',         'decided',      13,   7)
  on conflict (user_id, key) do nothing;
$$;

-- DEBUG_RETAIN_BODIES_DAYS defaults to 0 and is capped at 7. A nightly job
-- deletes anything past it and logs the count, because a retention promise
-- nobody sweeps is a retention promise nobody keeps.
create table if not exists debug_bodies (
  mailbox_id          uuid not null,
  provider_message_id text not null,
  user_id             uuid not null references users on delete cascade,
  body                text not null,
  stored_at           timestamptz not null default now(),
  primary key (mailbox_id, provider_message_id)
);
alter table debug_bodies enable row level security;
alter table debug_bodies force row level security;
create policy tenant on debug_bodies
  using (user_id = loop_current_user()) with check (user_id = loop_current_user());

create or replace function sweep_retention(p_days integer) returns integer
language plpgsql as $$
declare n integer;
begin
  if p_days > 7 then
    raise exception 'DEBUG_RETAIN_BODIES_DAYS is capped at 7, got %', p_days;
  end if;
  delete from debug_bodies where stored_at < now() - make_interval(days => p_days);
  get diagnostics n = row_count;

  -- Resolved review excerpts go with their item. What survives is the
  -- structural pattern only — no names, no addresses, no free text — so that
  -- rule-writing keeps improving without keeping anybody's correspondence.
  update review_items
     set excerpt = null
   where resolved_at is not null and excerpt is not null;

  return n;
end $$;

select cron.schedule('loop-retention', '30 3 * * *', $$select sweep_retention(0)$$);

-- A rebuild of the projection from the log alone. §04 invariant 6 asks for
-- exactly one function that does this, and a test asserts the rebuild is
-- byte-identical to what was there before.
create or replace function rebuild_application(p_app uuid) returns void
language plpgsql as $$
begin
  -- The fold itself lives in packages/domain as a pure function; this exists so
  -- the invariant is expressible in SQL for the test harness, which calls the
  -- TypeScript fold and writes the result back through the pipeline path.
  raise notice 'rebuild is driven from packages/db/src/rebuild.ts for %', p_app;
end $$;
