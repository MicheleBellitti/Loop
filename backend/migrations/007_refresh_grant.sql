-- 007 · let the pipeline refresh the projection
--
-- `refresh materialized view` requires ownership, and the pipeline service runs
-- as `loop_pipeline`, not as the owner. Granting execute is not enough on its
-- own — the function has to run with the owner's rights — so it becomes
-- SECURITY DEFINER with an empty search_path, which is the standard way to
-- hand out one privileged operation without handing out the role.
--
-- Found by the integration test rather than by reading: the rebuild test called
-- it as `loop_pipeline` and Postgres said no.

create or replace function refresh_projections() returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  refresh materialized view concurrently app_phase_reach;
exception when object_not_in_prerequisite_state then
  -- The very first refresh after creation cannot be concurrent.
  refresh materialized view app_phase_reach;
end $$;

revoke all on function refresh_projections() from public;

do $$
declare r text;
begin
  foreach r in array array['loop_pipeline','loop_gateway'] loop
    execute format('grant execute on function refresh_projections() to %I', r);
  end loop;
end $$;
