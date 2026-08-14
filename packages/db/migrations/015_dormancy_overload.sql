-- 015 · nothing has gone dormant since 2026-08-11
--
-- Migration 010 added `sweep_dormancy(p_presumed_closed_days integer default 90)`
-- without dropping the zero-argument `sweep_dormancy()` from 005. A default
-- argument does not replace an overload, it competes with one: `select
-- sweep_dormancy()`, which is what cron job 1 has been scheduled to run since
-- 005, now matches both candidates and Postgres refuses to choose.
--
--   ERROR:  function sweep_dormancy() is not unique
--   HINT:   Could not choose a best candidate function.
--
-- Ten successful runs before 010, then forty-two consecutive failures. The
-- product's central claim is that it notices silence for you, and it has not
-- noticed any since. This is exactly the class of failure a cron job hides:
-- nothing is logged where anyone looks, and "no application went dormant today"
-- is indistinguishable from a working sweep on a quiet week.

drop function if exists sweep_dormancy();

-- And say the argument out loud rather than leaning on the default, so that
-- adding a second parameter later is a syntax error here instead of a silent
-- change of meaning.
select cron.alter_job(
  (select jobid from cron.job where jobname = 'loop-dormancy'),
  command => $$select sweep_dormancy(90)$$
);
