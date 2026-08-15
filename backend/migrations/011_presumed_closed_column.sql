-- 011 · the flag the interface reads
--
-- Derived from the latest went_silent event's payload, so it is rebuilt by the
-- fold like everything else on this row and survives a `rebuildAll`.
alter table applications add column if not exists presumed_closed boolean not null default false;
