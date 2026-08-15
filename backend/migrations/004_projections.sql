-- 004 · projections
--
-- "Every metric is a view over the event log, refreshed by the pipeline after
-- each append (debounced 5 s), never computed in the client."
--
-- Two corrections to §11's DDL:
--   · the view inner-joins the event log, so an application with no events
--     disappears from the funnel *denominator* — the one place a missing row
--     silently flatters every ratio. It is a left join here. decisions.md B11.
--   · a materialised view without a unique index cannot be refreshed
--     concurrently, so a 5-second debounce would take an AccessExclusiveLock
--     and block every read on the box. decisions.md B10.

create materialized view app_phase_reach as
select
  a.id,
  a.user_id,
  a.company_id,
  a.applied_at,
  a.status,
  a.current_phase,
  coalesce(bool_or(
    e.type in ('interview_scheduled','interview_held') or sd.phase = 'interviewing'
  ), false) as reached_interview,
  coalesce(bool_or(e.type = 'offer_received'), false) as reached_offer,
  -- First *human* reply. §11 filters on `type in ('recruiter_reachout',...)`,
  -- but recruiter_reachout is a stage key, not an event type — that predicate
  -- can never be true. The honest definition, from the Architecture sheet §07,
  -- is the first inbound event that is not the application itself and not an
  -- automated acknowledgement. decisions.md B13.
  min(e.occurred_at) filter (
    where e.type in ('stage_advanced','interview_scheduled','interview_held',
                     'offer_received','offer_negotiated','rejected','deadline_set')
  ) as first_human_at,
  max(e.occurred_at) filter (where e.type <> 'went_silent') as last_event_at,
  count(e.id) as event_count
from applications a
left join application_events e on e.application_id = a.id
left join stage_defs sd on sd.user_id = a.user_id and sd.key = e.to_stage
where a.merged_into_id is null
group by a.id, a.user_id, a.company_id, a.applied_at, a.status, a.current_phase;

create unique index app_phase_reach_pk on app_phase_reach (id);
create index app_phase_reach_user_idx on app_phase_reach (user_id);

-- Dwell between consecutive stage changes, per user and per stage pair. This
-- feeds the nudge thresholds, so it must be the user's own cadence and not a
-- global average — "the app learns your real cadence".
create view stage_dwell as
select
  t.user_id,
  t.from_stage,
  t.to_stage,
  percentile_cont(0.5) within group (order by extract(epoch from t.gap) / 86400) as p50_days,
  percentile_cont(0.75) within group (order by extract(epoch from t.gap) / 86400) as p75_days,
  percentile_cont(0.9) within group (order by extract(epoch from t.gap) / 86400) as p90_days,
  count(*) as n
from (
  select
    user_id,
    application_id,
    lag(to_stage) over w as from_stage,
    to_stage,
    occurred_at - lag(occurred_at) over w as gap
  from application_events
  where type in ('stage_advanced','applied','acknowledged','interview_scheduled','offer_received')
  window w as (partition by application_id order by occurred_at, id)
) t
where t.gap is not null and t.to_stage is not null
group by t.user_id, t.from_stage, t.to_stage;

-- Dwell keyed on the stage an application sits *in*, which is what the
-- follow-up rule and the dormancy sweep actually ask for.
create view stage_dwell_in as
select user_id, from_stage as stage,
       percentile_cont(0.5) within group (order by days) as p50_days,
       percentile_cont(0.75) within group (order by days) as p75_days,
       percentile_cont(0.9) within group (order by days) as p90_days,
       count(*) as n
from (
  select user_id, from_stage, extract(epoch from gap) / 86400 as days
  from (
    select user_id,
           lag(to_stage) over w as from_stage,
           occurred_at - lag(occurred_at) over w as gap
    from application_events
    where type in ('stage_advanced','applied','acknowledged','interview_scheduled','offer_received')
    window w as (partition by application_id order by occurred_at, id)
  ) x
  where gap is not null and from_stage is not null
) y
group by user_id, from_stage;

-- Channel effectiveness, attributed to first touch. Referrals are their own row
-- and are never folded into a board — folding four of them into LinkedIn would
-- flatter it by six points.
create view channel_effectiveness as
select
  s.channel,
  r.user_id,
  count(*) as sent,
  count(*) filter (where r.reached_interview) as interviews,
  count(*) filter (where r.reached_offer) as offers,
  count(*) filter (where r.status = 'dormant') as ghosted
from sources s
join app_phase_reach r on r.id = s.application_id
where s.is_first_touch
group by s.channel, r.user_id;

-- The pipeline calls this after an append, debounced. Concurrently, so reads
-- never stall behind a refresh.
create or replace function refresh_projections() returns void
language plpgsql as $$
begin
  refresh materialized view concurrently app_phase_reach;
exception when object_not_in_prerequisite_state then
  -- The very first refresh after creation cannot be concurrent.
  refresh materialized view app_phase_reach;
end $$;
