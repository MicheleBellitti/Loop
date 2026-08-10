# Postgres 16 for Loop.
#
# The design wants one process to operate — no Redis, no Kafka, no separate
# scheduler — which means the queue, the scheduler and the vector index all live
# in Postgres. Two of the three come from the PGDG apt repository the base image
# already trusts:
#
#   pgvector — role-title embeddings for the resolver's cosine match
#   pg_cron  — the nightly dormancy sweep, the park drain, the nudge tick
#
# The third, pgmq, is not packaged there, and building it pulls in
# postgresql-server-dev-16 → llvm-19-dev: several hundred megabytes of compiler
# on an image meant for a free-tier ARM instance, plus a rebuild on every minor
# Postgres bump. So the queue is implemented in migration 005 as ~90 lines of
# SQL in an `mq` schema, exposing the same surface pgmq does (send, read with a
# visibility timeout, delete, archive, metrics) on a table with SKIP LOCKED.
# The spec's stated reason for choosing pgmq — "transactional with the data it
# describes; visibility timeouts are enough at this volume" — is satisfied
# identically, and swapping back is a search-and-replace of `mq.` for `pgmq.`.
# decisions.md B14.
FROM postgres:16-bookworm

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        postgresql-16-pgvector \
        postgresql-16-cron; \
    rm -rf /var/lib/apt/lists/*

# pg_cron must be preloaded and must name the database it schedules against.
RUN set -eux; \
    echo "shared_preload_libraries='pg_cron'" >> /usr/share/postgresql/postgresql.conf.sample; \
    echo "cron.database_name='loop'"          >> /usr/share/postgresql/postgresql.conf.sample
