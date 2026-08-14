-- 016 · an index for "how much did it place today"
--
-- The mailbox health object answers that count, and the shell polls it: every
-- two seconds during onboarding, on every window focus after that, and once
-- more on every `scan.progress` event. `/api/today` carries the same object, so
-- the count runs on the landing endpoint too.
--
-- `seen_messages` had nothing that could serve it. The primary key is
-- `(mailbox_id, provider_message_id)`, the only other index is partial on
-- `outcome = 'parked'`, and no index mentioned `user_id` at all — so the count
-- sequentially scanned the append-only replay log, the largest table in the
-- schema and the one being bulk-inserted into at exactly the moment onboarding
-- is asking. RLS filters after the scan rather than choosing the access path,
-- so it does not help either.
--
-- Partial on 'placed' to match the predicate exactly and to stay small: the
-- dropped, parked and review rows are the majority and none of them is counted.
create index if not exists seen_messages_placed_idx
    on seen_messages (user_id, processed_at)
 where outcome = 'placed';
