-- 012 · a stage for "an interview is scheduled, and we do not know which round"
--
-- `stageFromTitle` reads a calendar summary for keywords and defaulted every
-- unrecognised invite to `technical`. That is why the pipeline showed a column
-- of identical Technical stages: it was not a reading, it was a fallback wearing
-- a reading's clothes. The same default appears again in the resolver, as
-- `signal.stage_hint ?? 'technical'` on four lines.
--
-- An invitation whose title names no round is real evidence that the process has
-- reached interviewing, and no evidence at all about which interview it is. So
-- the claim the evidence supports gets a stage of its own, at the front of the
-- interviewing band, and the rounds below it move up one to make room. Nothing
-- reads depth except display order, "how far did it get", and the headline's
-- test for forward movement, so renumbering is a presentation change.
--
-- Depth 11 was one of the two the original set left free for exactly this.
-- Depths 0 and 3 remain free.

insert into stage_defs (user_id, key, label, phase, depth, stale_after_days)
select id, 'interview', 'Interview', 'interviewing', 7, 12 from users
on conflict (user_id, key) do nothing;

update stage_defs set depth = 8  where key = 'technical'     and depth = 7;
update stage_defs set depth = 9  where key = 'system_design' and depth = 8;
update stage_defs set depth = 10 where key = 'onsite_loop'   and depth = 9;
update stage_defs set depth = 11 where key = 'final'         and depth = 10;

create or replace function seed_stage_defs(p_user uuid) returns void
language sql as $$
  insert into stage_defs (user_id, key, label, phase, depth, stale_after_days)
  values
    (p_user, 'applied',            'Applied',             'sent',          1,  21),
    (p_user, 'acknowledged',       'Acknowledged',        'sent',          2,  21),
    (p_user, 'recruiter_reachout', 'Recruiter reach-out', 'screening',     4,  10),
    (p_user, 'hr_call',            'HR call',             'screening',     5,  10),
    (p_user, 'take_home',          'Take-home',           'screening',     6,  14),
    (p_user, 'interview',          'Interview',           'interviewing',  7,  12),
    (p_user, 'technical',          'Technical',           'interviewing',  8,  12),
    (p_user, 'system_design',      'System design',       'interviewing',  9,  12),
    (p_user, 'onsite_loop',        'Onsite loop',         'interviewing', 10,  14),
    (p_user, 'final',              'Final',               'interviewing', 11,  10),
    (p_user, 'offer',              'Offer',               'decided',      12,   7),
    (p_user, 'negotiating',        'Negotiating',         'decided',      13,   7)
  on conflict (user_id, key) do nothing;
$$;
