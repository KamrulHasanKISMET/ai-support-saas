#!/usr/bin/env bash
# Physical base backup via `pg_basebackup`, taken over the replication
# protocol against the running `postgres` service. Paired with the
# continuous WAL archive (Postgres's own `archive_command`, configured
# on the `postgres` service in docker-compose.yml, writing into the
# shared `wal_archive` volume) this is what makes point-in-time
# recovery possible: restore this base backup, then replay archived
# WAL up to any timestamp between this backup and now. See
# docs/RELIABILITY.md "PITR design" and pitr_restore.sh.
#
# Requires the `postgres` service to have a replication-capable role
# available; REPLICATION_USER/REPLICATION_PASSWORD default to the
# existing app superuser (fine for a single-instance dev/small-prod
# setup -- see docs/RELIABILITY.md for tightening this with a
# dedicated replication role before scaling to a real HA setup).
set -euo pipefail

BASE_BACKUP_DIR="${BASE_BACKUP_DIR:-/backup/base}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
METRICS_DIR="${METRICS_TEXTFILE_DIR:-/backup/metrics}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET_DIR="${BASE_BACKUP_DIR}/${TIMESTAMP}"

mkdir -p "${BASE_BACKUP_DIR}" "${METRICS_DIR}"

write_metric() {
  local status="$1"
  local tmp
  tmp="$(mktemp "${METRICS_DIR}/.pg_base_backup.XXXXXX")"
  {
    echo "# HELP pg_base_backup_last_run_timestamp_seconds Unix time of the last pg_basebackup attempt"
    echo "# TYPE pg_base_backup_last_run_timestamp_seconds gauge"
    echo "pg_base_backup_last_run_timestamp_seconds $(date +%s)"
    echo "# HELP pg_base_backup_last_success Whether the last pg_basebackup attempt succeeded (1) or failed (0)"
    echo "# TYPE pg_base_backup_last_success gauge"
    echo "pg_base_backup_last_success ${status}"
  } > "${tmp}"
  mv "${tmp}" "${METRICS_DIR}/pg_base_backup.prom"
}

echo "[pg_base_backup] $(date -Iseconds) -- base backup -> ${TARGET_DIR}"

if ! pg_basebackup -h "${PGHOST}" -U "${PGUSER}" -D "${TARGET_DIR}" -Fp -Xs -P -c fast; then
  echo "[pg_base_backup] pg_basebackup FAILED" >&2
  write_metric 0
  exit 1
fi

# Retention on base backups (directories, not files -- prune by mtime
# of a marker file written by pg_basebackup into each backup dir).
find "${BASE_BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -print0 \
  | xargs -0 -r rm -rf

if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  if command -v aws > /dev/null 2>&1; then
    aws ${BACKUP_S3_ENDPOINT:+--endpoint-url "${BACKUP_S3_ENDPOINT}"} \
      s3 sync "${TARGET_DIR}" "s3://${BACKUP_S3_BUCKET}/base/${TIMESTAMP}/"
    echo "[pg_base_backup] synced to s3://${BACKUP_S3_BUCKET}/base/${TIMESTAMP}/"
  else
    echo "[pg_base_backup] BACKUP_S3_BUCKET is set but the aws CLI is not installed in this image -- see ops/backup/README.md" >&2
    write_metric 0
    exit 1
  fi
else
  echo "[pg_base_backup] BACKUP_S3_BUCKET not set -- local-only backup (fine for dev, NOT fine for production)"
fi

write_metric 1
echo "[pg_base_backup] done"
