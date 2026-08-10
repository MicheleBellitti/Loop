-- 001 · extensions and the id generator
--
-- Engineering Spec §04 specifies `uuid_generate_v7()` as the default for every
-- primary key. No such function exists in stock Postgres 16 — core `uuidv7()`
-- only arrives in 18 — so it is implemented here rather than pulled in as one
-- more extension to keep installed. decisions.md B3.

create extension if not exists citext;
create extension if not exists pgcrypto;
create extension if not exists vector;

-- RFC 9562 layout: 48 bits of Unix milliseconds, version 7, 74 bits of random.
-- Time-ordered, so index locality is good and rows sort by creation without a
-- separate column.
create or replace function uuid_generate_v7() returns uuid
language plpgsql parallel safe as $$
declare
  unix_ts_ms bytea;
  uuid_bytes bytea;
begin
  unix_ts_ms := substring(int8send((extract(epoch from clock_timestamp()) * 1000)::bigint) from 3);
  uuid_bytes := unix_ts_ms || gen_random_bytes(10);
  -- Version 7 in the high nibble of byte 6, variant 0b10 in the top two bits of
  -- byte 8. Written as integer masks rather than the bit-string concatenation
  -- that circulates for this function: `||` and `>>` sit at the same precedence
  -- in Postgres and associate left, so `b'0111' || x >> 4` shifts the *joined*
  -- string and quietly produces a version-0 UUID.
  uuid_bytes := set_byte(uuid_bytes, 6, (get_byte(uuid_bytes, 6) & 15)  | 112);
  uuid_bytes := set_byte(uuid_bytes, 8, (get_byte(uuid_bytes, 8) & 63)  | 128);
  return encode(uuid_bytes, 'hex')::uuid;
end $$;

comment on function uuid_generate_v7() is
  'UUIDv7 per RFC 9562. Replace with core uuidv7() when this box reaches PG18.';

-- Confidence is a number every event carries, so it is a domain rather than a
-- convention. §04 writes `create type confidence as numeric(3,2)`, which is not
-- valid SQL — CREATE TYPE has no such form. decisions.md B1.
do $$ begin
  create domain confidence as numeric(3,2)
    check (value >= 0 and value <= 1);
exception when duplicate_object then null; end $$;
