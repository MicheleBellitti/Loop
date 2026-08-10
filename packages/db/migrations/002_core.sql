-- 002 · the core schema
--
-- Engineering Spec §04 is normative and this is it, with the twelve corrections
-- listed in docs/decisions.md §B. `applications` is a projection: every column
-- except id, user_id and created_at is derived from the event log and MAY be
-- rebuilt at any time.

create type phase as enum ('sent','screening','interviewing','decided');
create type app_status as enum ('live','dormant','rejected','withdrawn','accepted');
create type channel as enum ('linkedin','indeed','career_page','referral','recruiter','other');

create table users (
  id          uuid primary key default uuid_generate_v7(),
  email       citext unique not null,
  -- A setting, not the device zone: the 03:00 dormancy sweep and the 21:00
  -- quiet boundary must not move every time the user travels. decisions.md D4.
  tz          text not null default 'Europe/Rome',
  display_currency char(3) not null default 'EUR',
  locale      text not null default 'en-GB',
  created_at  timestamptz not null default now()
);

-- Companies are global on purpose: "this domain belongs to this company" is a
-- public fact, not user data. Learned aliases are not — they move to a
-- user-scoped table below, or a name one user teaches leaks to the next.
-- §04 declares `unique (canonical_name)` alongside `unique (domain)`, which
-- makes two same-named companies at different domains impossible. decisions.md B7.
create table companies (
  id             uuid primary key default uuid_generate_v7(),
  canonical_name text not null,
  domain         citext unique,
  industry       text,
  size_bucket    text,
  created_at     timestamptz not null default now()
);
create unique index companies_name_domain_key
  on companies (lower(canonical_name), coalesce(domain, ''));

create table company_aliases (
  user_id    uuid not null references users on delete cascade,
  company_id uuid not null references companies on delete cascade,
  alias      text not null,          -- already normalised by normaliseCompany()
  source     text not null default 'resolver',  -- resolver | review | manual
  created_at timestamptz not null default now(),
  primary key (user_id, alias)
);

create table stage_defs (
  user_id          uuid not null references users on delete cascade,
  key              text not null,
  label            text not null,
  phase            phase not null,
  depth            smallint not null,
  stale_after_days smallint not null,
  primary key (user_id, key)
);

-- `sources` is declared before `application_events` because that table carries
-- a foreign key to it; §04 declares them the other way round and will not run.
-- decisions.md B2.
create table applications (
  id                     uuid primary key default uuid_generate_v7(),
  user_id                uuid not null references users on delete cascade,
  company_id             uuid not null references companies,
  role_title             text not null,
  role_normalised        text,
  role_embedding         vector(384),
  seniority              text,
  location               text,
  work_mode              text check (work_mode in ('onsite','hybrid','remote')),
  current_stage          text not null,   -- FK-free: stages are user-configurable
  current_phase          phase not null,
  status                 app_status not null default 'live',
  applied_at             timestamptz,
  last_signal_at         timestamptz,
  went_dormant_at        timestamptz,
  last_user_action_at    timestamptz,
  -- True when the ball is in their court; drives the follow-up rule.
  awaiting_them          boolean not null default true,
  comp_expectation_minor bigint,
  comp_currency          char(3),
  confidence             confidence not null default 1.0,
  needs_review           boolean not null default false,
  -- Set when this application was created by hand. A user-declared application
  -- is authoritative and is never auto-merged into another (Spec §09).
  manually_created       boolean not null default false,
  merged_into_id         uuid references applications on delete set null,
  created_at             timestamptz not null default now()
);
create index applications_user_status_signal_idx
  on applications (user_id, status, last_signal_at desc);
create index applications_user_company_idx on applications (user_id, company_id);
create index applications_embedding_idx
  on applications using hnsw (role_embedding vector_cosine_ops);

create table sources (
  id             uuid primary key default uuid_generate_v7(),
  user_id        uuid not null references users on delete cascade,
  application_id uuid not null references applications on delete cascade,
  channel        channel not null,
  posting_url    text,
  ats_vendor     text,
  first_seen_at  timestamptz not null default now(),
  is_first_touch boolean not null default false
);
create index sources_application_idx on sources (application_id);
-- Exactly one first touch per application — every channel statistic depends on
-- it, so the database holds the invariant rather than the code promising to.
create unique index sources_one_first_touch
  on sources (application_id) where is_first_touch;

create table application_events (       -- append only. no UPDATE, no DELETE
  id             bigserial primary key,
  application_id uuid not null references applications on delete cascade,
  user_id        uuid not null references users on delete cascade,
  type           text not null,
  occurred_at    timestamptz not null,  -- when it happened in the world
  recorded_at    timestamptz not null default now(),  -- when we learned it
  from_stage     text,
  to_stage       text,
  payload        jsonb not null default '{}',
  source_id      uuid references sources on delete set null,
  confidence     confidence not null,
  evidence_ref   text,                  -- provider message id. never a body
  rung           smallint check (rung between 1 and 4),
  constraint application_events_type_known check (type in (
    'applied','acknowledged','stage_advanced','interview_scheduled',
    'interview_held','deadline_set','offer_received','offer_negotiated',
    'rejected','withdrawn','accepted','went_silent','human_corrected',
    'note_added'
  ))
);
-- Idempotency. §04 writes a plain UNIQUE, which does nothing when evidence_ref
-- is null because nulls are distinct by default — and every human-authored
-- event has a null evidence_ref. decisions.md B4.
create unique index application_events_idempotency
  on application_events (application_id, type, occurred_at, evidence_ref)
  nulls not distinct;
create index application_events_user_time_idx on application_events (user_id, occurred_at desc);
create index application_events_app_idx on application_events (application_id, occurred_at);

create table mailbox_accounts (
  id                uuid primary key default uuid_generate_v7(),
  user_id           uuid not null references users on delete cascade,
  provider          text not null,   -- gmail | google_calendar (imap ships later)
  address           citext not null,
  secret_ciphertext bytea not null,  -- sealed with the per-user DEK
  secret_nonce      bytea not null,
  dek_wrapped       bytea not null,  -- DEK sealed with LOOP_KEK
  dek_nonce         bytea not null,
  scopes            text[] not null default '{}',
  -- Opaque so a second provider is a new connector module, not a migration.
  cursor            jsonb not null default '{}',
  watch_expires_at  timestamptz,
  status            text not null default 'ok',  -- ok | needs_reauth | error | paused
  last_ok_at        timestamptz,
  last_error        text,
  backlog_estimate  integer not null default 0,
  created_at        timestamptz not null default now(),
  unique (user_id, provider, address)
);

-- The replay log. Survives everything downstream, which is what makes the box
-- safe to be down for a day.
create table seen_messages (
  mailbox_id          uuid not null references mailbox_accounts on delete cascade,
  provider_message_id text not null,
  -- Denormalised from the mailbox so a row-level policy can be written at all.
  user_id             uuid not null references users on delete cascade,
  body_sha256         bytea not null,
  received_at         timestamptz not null,
  processed_at        timestamptz,
  outcome             text,   -- placed | dropped | parked | review
  park_attempts       smallint not null default 0,
  primary key (mailbox_id, provider_message_id)
);
create index seen_messages_parked_idx
  on seen_messages (outcome, processed_at) where outcome = 'parked';

create table interviews (
  id                uuid primary key default uuid_generate_v7(),
  user_id           uuid not null references users on delete cascade,
  application_id    uuid not null references applications on delete cascade,
  stage             text not null,
  starts_at         timestamptz not null,
  ends_at           timestamptz,
  location          text,
  calendar_event_id text,
  held              boolean,
  cancelled_at      timestamptz,
  -- §04 makes calendar_event_id globally unique, which collides across tenants.
  unique (user_id, calendar_event_id)
);
create index interviews_upcoming_idx on interviews (user_id, starts_at);

create table comp_offers (
  id              uuid primary key default uuid_generate_v7(),
  user_id         uuid not null references users on delete cascade,
  application_id  uuid not null references applications on delete cascade,
  kind            text not null check (kind in ('posted_range','ask','offer')),
  min_minor       bigint,
  max_minor       bigint,
  currency        char(3) not null,
  equity_note     text,
  decide_by       date,
  source_event_id bigint references application_events on delete set null,
  created_at      timestamptz not null default now()
);
create index comp_offers_application_idx on comp_offers (application_id);

create table deadlines (
  id              uuid primary key default uuid_generate_v7(),
  user_id         uuid not null references users on delete cascade,
  application_id  uuid not null references applications on delete cascade,
  kind            text not null,
  due_at          timestamptz not null,
  url             text,
  source          text not null default 'gmail',
  met_at          timestamptz,
  source_event_id bigint references application_events on delete set null
);
create index deadlines_due_idx on deadlines (user_id, due_at) where met_at is null;

create table review_items (
  id           uuid primary key default uuid_generate_v7(),
  user_id      uuid not null references users on delete cascade,
  kind         text not null check (kind in
                 ('ambiguous_match','unknown_intent','low_confidence','merge_undo')),
  evidence_ref text not null,
  -- Set once the item is about a known application, so `needs_review` on the
  -- row can be derived rather than remembered.
  application_id uuid references applications on delete cascade,
  -- The single exception to "no table ever stores message bodies": ≤280 chars,
  -- redacted, display-only, deleted with the item.
  excerpt      text check (char_length(excerpt) <= 280),
  candidates   jsonb not null default '[]',
  -- Kept after resolution so rule-writing improves; carries no free text and no
  -- names, only the structural shape of what matched. decisions.md D6.
  learned_pattern jsonb,
  created_at   timestamptz not null default now(),
  resolved_at  timestamptz,
  resolution   jsonb,
  expires_at   timestamptz
);
create index review_items_open_idx on review_items (user_id, created_at)
  where resolved_at is null;

-- Suggestions are durable so "one per application per rule, ever, unless it
-- expired and re-triggered" can actually be enforced across restarts.
create table suggestions (
  id             uuid primary key default uuid_generate_v7(),
  user_id        uuid not null references users on delete cascade,
  key            text not null,
  rule           text not null,
  application_ids uuid[] not null default '{}',
  payload        jsonb not null default '{}',
  created_at     timestamptz not null default now(),
  expires_at     timestamptz,
  acted_at       timestamptz,
  snoozed_until  timestamptz,
  dismissed_at   timestamptz,
  unique (user_id, key)
);
create index suggestions_open_idx on suggestions (user_id)
  where acted_at is null and dismissed_at is null;

create table push_subscriptions (
  id         uuid primary key default uuid_generate_v7(),
  user_id    uuid not null references users on delete cascade,
  endpoint   text not null,
  p256dh     text not null,
  auth       text not null,
  created_at timestamptz not null default now(),
  unique (user_id, endpoint)
);

create table notifications_sent (
  id         uuid primary key default uuid_generate_v7(),
  user_id    uuid not null references users on delete cascade,
  rule       text not null,
  suggestion_key text,
  sent_at    timestamptz not null default now(),
  -- The daily cap counts calendar days in the user's own timezone.
  local_date date not null
);
create index notifications_sent_day_idx on notifications_sent (user_id, local_date);

-- WebAuthn credentials plus the recovery password. decisions.md OPEN-4.
create table credentials (
  id              uuid primary key default uuid_generate_v7(),
  user_id         uuid not null references users on delete cascade,
  credential_id   text not null unique,
  public_key      bytea not null,
  counter         bigint not null default 0,
  transports      text[] not null default '{}',
  label           text,
  created_at      timestamptz not null default now(),
  last_used_at    timestamptz
);

create table auth_secrets (
  user_id            uuid primary key references users on delete cascade,
  recovery_hash      text not null,        -- argon2id
  recovery_used_at   timestamptz,
  webauthn_challenge text,
  challenge_expires_at timestamptz
);

create table sessions (
  id         uuid primary key default uuid_generate_v7(),
  user_id    uuid not null references users on delete cascade,
  token_hash bytea not null unique,
  csrf_hash  bytea not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  last_seen_at timestamptz not null default now()
);
create index sessions_user_idx on sessions (user_id);

-- Consent records, so "the scope list version stored alongside the timestamp"
-- is a row rather than a promise (Spec §15).
create table consents (
  id           uuid primary key default uuid_generate_v7(),
  user_id      uuid not null references users on delete cascade,
  kind         text not null,       -- mailbox_scopes | hosted_model
  version      text not null,
  detail       jsonb not null default '{}',
  granted_at   timestamptz not null default now(),
  revoked_at   timestamptz
);
