-- 014 · the grants row-level security has never actually needed
--
-- None of the queue-driven TypeScript services issue `set local role`. They
-- connect as the owner, which is a superuser, so every policy, every FORCE flag
-- and every grant in 003 is decorative on those paths — `withUser` in the
-- gateway is the only place a service role was ever assumed.
--
-- The Python port issues `set local role` everywhere, which is the first time
-- these grants are load-bearing. Four routes and one rung then fail with 42501
-- on statements that have shipped for months. Each one below is a decision about
-- who is allowed to write what, not a transcription of what the code happens to
-- do today.

-- The extractor raises rung-4 review items — 003 granted that to the resolver
-- and the gateway and not to the service whose whole fourth rung is asking a
-- human. First unreadable message, permission denied.
grant insert on review_items to loop_extractor;

-- Quick add is the one place the user, not the mailbox, is the source of truth,
-- so the gateway must be able to bring an application into existence and hand
-- back its id in the same request.
--
-- INSERT and nothing more, deliberately. "Only the pipeline changes application
-- state" survives intact: the gateway may create a row, and every subsequent
-- move of it still goes through the event log. Granting UPDATE here would make
-- `applications` a projection with two writers, which is the thing these grants
-- exist to prevent.
grant insert on companies    to loop_gateway;
grant insert on applications to loop_gateway;
grant insert on comp_offers  to loop_gateway;

-- Disconnecting a mailbox is a gateway operation and there is nowhere else for
-- it to happen.
grant delete on mailbox_accounts to loop_gateway;

-- `mq.extend` (013) is the only queue function with no explicit grant: 005 ran
-- `grant execute on all functions in schema mq` once, and there is no
-- `alter default privileges` behind it. It works today purely because Postgres
-- grants EXECUTE to PUBLIC by default — so the first `revoke execute … from
-- public` would silently stop lease extension, and a batch of twenty would go
-- back to being worked twice. The default privileges below close the same trap
-- for whatever gets added to `mq` next.
do $$
declare r text;
begin
  foreach r in array array[
    'loop_gateway','loop_connector','loop_classifier','loop_extractor',
    'loop_resolver','loop_pipeline','loop_nudge','loop_notifier'
  ] loop
    execute format('grant execute on all functions in schema mq to %I', r);
    execute format(
      'alter default privileges in schema mq grant execute on functions to %I', r);
  end loop;
end $$;

-- `erase_user` is SECURITY DEFINER owned by a superuser and resolves every name
-- in it through the caller's `search_path`. On PG16 the public schema no longer
-- grants CREATE to PUBLIC, so this is not exploitable as it stands — it is one
-- `grant create on schema public` away from being a privilege escalation, which
-- is not a distance worth leaving. `refresh_projections()` already pins its own.
alter function erase_user(uuid) set search_path = public, pg_temp;
