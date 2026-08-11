-- 010 · silence, second tier
--
-- The nightly sweep already marks an application `dormant` past its stage's
-- staleness. This adds the longer judgement: past PRESUMED_CLOSED_DAYS with no
-- inbound signal at all, you were passed over without being told.
--
-- It stays a `went_silent` event and the status stays `dormant` — inventing a
-- `rejected` here would corrupt the ghost rate, which is defined precisely as
-- "went dormant *without* an explicit rejection ÷ closed". What changes is the
-- payload flag, which the interface reads to say "closed by silence" instead of
-- "quiet", and which stops the let-it-go nudge from asking about it forever.

create or replace function sweep_dormancy(p_presumed_closed_days integer default 90)
returns integer language plpgsql as $$
declare n integer := 0; rec record; presumed boolean;
begin
  for rec in
    select a.id, a.user_id, a.current_stage, a.last_signal_at, a.status,
           greatest(coalesce(sd.stale_after_days, 21), coalesce(2 * d.p90_days, 0)) as threshold_days,
           case when d.n is not null then 'p90x2' else 'stale_after_days' end as threshold_used,
           extract(epoch from (now() - a.last_signal_at)) / 86400 as quiet_days
    from applications a
    join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
    left join stage_dwell_in d
           on d.user_id = a.user_id and d.stage = a.current_stage and d.n >= 5
    where a.status in ('live','dormant')
      and a.merged_into_id is null
      and a.last_signal_at is not null
      and a.user_id in (select dormancy_due_users())
  loop
    -- A take-home, an open offer or a negotiation is waiting on *you*. Silence
    -- there is not a verdict, it is a task, and the deadline rule owns it.
    presumed := rec.quiet_days > p_presumed_closed_days
                and rec.current_stage not in ('take_home','offer','negotiating');

    -- Nothing to do if it is already dormant and not newly presumed closed.
    continue when rec.status = 'dormant' and not presumed;
    continue when rec.quiet_days <= rec.threshold_days and not presumed;

    perform mq.send('event_pending', jsonb_build_object(
      'user_id', rec.user_id,
      'application_id', rec.id,
      'event', jsonb_build_object(
        'type', 'went_silent',
        'occurred_at', now(),
        'confidence', 0.9,
        'payload', jsonb_build_object(
          'days_quiet', floor(rec.quiet_days),
          'threshold_used', case when presumed then 'presumed_closed' else rec.threshold_used end,
          'presumed_closed', presumed
        )
      )
    ));
    n := n + 1;
  end loop;
  return n;
end $$;

-- Run it now for every user rather than only at 03:00 local, so a box that has
-- just been brought up does not show a pipeline full of dead processes for a
-- day before the first sweep.
create or replace function sweep_dormancy_all(p_presumed_closed_days integer default 90)
returns integer language plpgsql as $$
declare n integer := 0; rec record; presumed boolean;
begin
  for rec in
    select a.id, a.user_id, a.current_stage, a.last_signal_at, a.status,
           greatest(coalesce(sd.stale_after_days, 21), coalesce(2 * d.p90_days, 0)) as threshold_days,
           case when d.n is not null then 'p90x2' else 'stale_after_days' end as threshold_used,
           extract(epoch from (now() - a.last_signal_at)) / 86400 as quiet_days
    from applications a
    join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
    left join stage_dwell_in d
           on d.user_id = a.user_id and d.stage = a.current_stage and d.n >= 5
    where a.status in ('live','dormant')
      and a.merged_into_id is null
      and a.last_signal_at is not null
  loop
    presumed := rec.quiet_days > p_presumed_closed_days
                and rec.current_stage not in ('take_home','offer','negotiating');
    continue when rec.status = 'dormant' and not presumed;
    continue when rec.quiet_days <= rec.threshold_days and not presumed;
    perform mq.send('event_pending', jsonb_build_object(
      'user_id', rec.user_id,
      'application_id', rec.id,
      'event', jsonb_build_object(
        'type', 'went_silent',
        'occurred_at', now(),
        'confidence', 0.9,
        'payload', jsonb_build_object(
          'days_quiet', floor(rec.quiet_days),
          'threshold_used', case when presumed then 'presumed_closed' else rec.threshold_used end,
          'presumed_closed', presumed
        )
      )
    ));
    n := n + 1;
  end loop;
  return n;
end $$;
