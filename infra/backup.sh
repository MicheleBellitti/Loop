#!/usr/bin/env bash
# Nightly encrypted dump. The KEK is never in the backup: restoring gives you
# every application and every event, and the mailboxes have to be reconnected.
# That is stated in the runbook on purpose rather than discovered during one.
set -euo pipefail

: "${BACKUP_AGE_RECIPIENT:?set BACKUP_AGE_RECIPIENT (an age public key)}"
DEST="${BACKUP_DEST:-./backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$DEST"

docker compose -f "$(dirname "$0")/../compose.yaml" exec -T postgres \
  pg_dump -U loop -d loop --format=custom --no-owner \
  | age -r "$BACKUP_AGE_RECIPIENT" -o "$DEST/loop-$STAMP.dump.age"

echo "wrote $DEST/loop-$STAMP.dump.age"

# Rotation. `-mtime` counts whole days, which is what the 30-day promise means.
find "$DEST" -name 'loop-*.dump.age' -mtime "+$KEEP_DAYS" -print -delete

if [ -n "${BACKUP_REMOTE:-}" ]; then
  # rclone to R2 or B2, both of which have a free tier that covers this.
  rclone copy "$DEST/loop-$STAMP.dump.age" "$BACKUP_REMOTE"
  echo "uploaded to $BACKUP_REMOTE"
fi
