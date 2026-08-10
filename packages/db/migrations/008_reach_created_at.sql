-- 008 · carry created_at into the reach projection
--
-- The period window falls back to `created_at` when an application has no
-- `applied_at` — an application whose only signal was an ATS acknowledgement
-- was still sent, and filtering it out shrinks the funnel's *denominator*,
-- which is the one direction a funnel must never be wrong in.
--
-- `app_phase_reach` did not carry the column, so the fallback had nothing to
-- reach for. Found by running a real backfill and reading the funnel.

-- `channel_effectiveness` reads from it, so the drop has to take it too and
-- the view is recreated below.
drop materialized view app_phase_reach cascade;

create materialized view app_phase_reach as
select
  a.id,
  a.user_id,
  a.company_id,
  a.applied_at,
  a.created_at,
  a.status,
  a.current_phase,
  coalesce(bool_or(
    e.type in ('interview_scheduled','interview_held') or sd.phase = 'interviewing'
  ), false) as reached_interview,
  coalesce(bool_or(e.type = 'offer_received'), false) as reached_offer,
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
group by a.id, a.user_id, a.company_id, a.applied_at, a.created_at, a.status, a.current_phase;

create unique index app_phase_reach_pk on app_phase_reach (id);
create index app_phase_reach_user_idx on app_phase_reach (user_id);

-- The view was dropped, so the grants and the dependent view go with it.
grant select on app_phase_reach to
  loop_gateway, loop_connector, loop_classifier, loop_extractor,
  loop_resolver, loop_pipeline, loop_nudge, loop_notifier;

create or replace view channel_effectiveness as
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

grant select on channel_effectiveness to
  loop_gateway, loop_connector, loop_classifier, loop_extractor,
  loop_resolver, loop_pipeline, loop_nudge, loop_notifier;
