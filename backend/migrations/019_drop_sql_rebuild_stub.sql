-- The `rebuild_application(uuid)` stub from migration 006 comes out.
--
-- It never rebuilt anything: it raised a notice saying the rebuild was driven
-- from `packages/db/src/rebuild.ts`, a file this port deletes. What it does do
-- is share a name with `loop.db.rebuild.rebuild_application`, so an operator
-- following §04 — "one function that does this" — and running
-- `select rebuild_application('<uuid>')` got a notice about a file that is not
-- there and a projection that had not moved.
--
-- A new migration rather than an edit to 006, because applied migrations are
-- immutable by checksum, which is the property that makes them trustworthy.

drop function if exists rebuild_application(uuid);
