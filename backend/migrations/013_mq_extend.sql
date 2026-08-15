-- 013 · extending a claim, so the lease matches the work
--
-- `mq.read` grants a visibility timeout once, for the whole batch, at the
-- moment of the claim. A consumer then works the batch one message at a time.
-- Those two facts do not fit: claim twenty messages on a sixty-second lease,
-- spend three seconds on each, and the twentieth message became visible again
-- before its handler started. Another consumer picks it up, both run it, and
-- its `read_ct` climbs on deliveries no handler ever saw — so it dead-letters
-- early, having been processed twice.
--
-- The alternatives were to claim one message at a time, which turns one round
-- trip into twenty, or to make the lease long enough for the worst batch, which
-- is also how long a crashed consumer's work stays stuck. Neither is a good
-- trade against eight lines of SQL that let a consumer say "still working on
-- this one".
--
-- Deliberately not an ack and not a nack: it only moves `vt`, and only for a
-- message the caller already holds.

create function mq.extend(p_queue text, p_msg_id bigint, p_vt integer)
returns boolean language sql as $$
  with touched as (
    update mq.messages
       set vt = now() + make_interval(secs => p_vt)
     where queue = p_queue and msg_id = p_msg_id
    returning 1
  ) select count(*) > 0 from touched;
$$;
