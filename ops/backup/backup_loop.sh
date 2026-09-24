#!/usr/bin/env bash
# Entrypoint for the `pg_backup` container (docker-compose.yml).
#
# There's no cron in the postgres base image, and adding one is more
# moving parts than this needs -- a plain sleep loop is easier to read,
# easier to debug (`docker logs saas_pg_backup`), and just as reliable
# for a single-instance Postgres. See docs/RELIABILITY.md.
#
# Runs, in order, on startup and then every BACKUP_INTERVAL_HOURS:
#   1. pg_backup.sh       -- logical dump (pg_dump -Fc), retention-pruned
#   2. pg_base_backup.sh  -- physical base backup (for PITR), retention-pruned
#
# Continuous WAL archiving is NOT done here -- it's Postgres's own
# `archive_command` (set on the `postgres` service in docker-compose.yml),
# writing directly to the shared `wal_archive` volume as each segment
# fills, independent of this container's schedule.
set -euo pipefail

INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[backup_loop] starting. Interval: every ${INTERVAL_HOURS}h. Retention: ${BACKUP_RETENTION_DAYS:-7}d."

while true; do
  echo "[backup_loop] $(date -Iseconds) -- running logical backup"
  if ! "${SCRIPT_DIR}/pg_backup.sh"; then
    echo "[backup_loop] $(date -Iseconds) -- LOGICAL BACKUP FAILED (see output above)" >&2
  fi

  echo "[backup_loop] $(date -Iseconds) -- running base backup"
  if ! "${SCRIPT_DIR}/pg_base_backup.sh"; then
    echo "[backup_loop] $(date -Iseconds) -- BASE BACKUP FAILED (see output above)" >&2
  fi

  echo "[backup_loop] $(date -Iseconds) -- pruning WAL archive"
  if ! "${SCRIPT_DIR}/prune_wal_archive.sh"; then
    echo "[backup_loop] $(date -Iseconds) -- WAL PRUNE FAILED (see output above)" >&2
  fi

  echo "[backup_loop] $(date -Iseconds) -- sleeping ${INTERVAL_HOURS}h until next run"
  sleep "$((INTERVAL_HOURS * 3600))"
done
