#!/usr/bin/env bash
# Logical backup: `pg_dump -Fc` (custom format -- compressed, supports
# selective/parallel restore via pg_restore, and is faster to sanity-
# check than a plain SQL dump). This is the fast path for "restore the
# whole DB" or "restore one table"; base_backup + WAL is the path for
# point-in-time recovery (see pg_base_backup.sh / docs/RELIABILITY.md).
#
# Runs inside the `pg_backup` container (docker-compose.yml), which has
# PGHOST/PGUSER/PGPASSWORD/PGDATABASE set via environment and the
# `pg_backups` volume mounted at $BACKUP_DIR.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backup/dumps}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
METRICS_DIR="${METRICS_TEXTFILE_DIR:-/backup/metrics}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_FILE="${BACKUP_DIR}/${PGDATABASE:-ai_support_saas}_${TIMESTAMP}.dump"

mkdir -p "${BACKUP_DIR}" "${METRICS_DIR}"

write_metric() {
  # node_exporter textfile-collector format: `metric_name value`.
  # Written atomically (temp file + mv) so node_exporter never reads a
  # half-written file mid-scrape.
  local status="$1"
  local tmp
  tmp="$(mktemp "${METRICS_DIR}/.pg_backup.XXXXXX")"
  {
    echo "# HELP pg_backup_last_run_timestamp_seconds Unix time of the last pg_dump attempt"
    echo "# TYPE pg_backup_last_run_timestamp_seconds gauge"
    echo "pg_backup_last_run_timestamp_seconds $(date +%s)"
    echo "# HELP pg_backup_last_success Whether the last pg_dump attempt succeeded (1) or failed (0)"
    echo "# TYPE pg_backup_last_success gauge"
    echo "pg_backup_last_success ${status}"
  } > "${tmp}"
  mv "${tmp}" "${METRICS_DIR}/pg_backup.prom"
}

echo "[pg_backup] $(date -Iseconds) -- dumping ${PGDATABASE:-ai_support_saas} -> ${DUMP_FILE}"

if ! pg_dump -Fc -f "${DUMP_FILE}"; then
  echo "[pg_backup] pg_dump FAILED" >&2
  write_metric 0
  exit 1
fi

# Integrity check: `pg_restore --list` parses the archive's TOC without
# writing anything anywhere -- catches a truncated/corrupt dump file
# (e.g. disk full mid-write) immediately, rather than at restore time
# during an actual incident. This is NOT the same as the full
# restore/verification drill (restore_drill.sh) -- that one actually
# restores into a scratch database, which this cheap check does not.
if ! pg_restore --list "${DUMP_FILE}" > /dev/null; then
  echo "[pg_backup] integrity check FAILED -- pg_restore --list could not read ${DUMP_FILE}" >&2
  write_metric 0
  exit 1
fi

echo "[pg_backup] integrity check OK"

# Retention: delete local dumps older than RETENTION_DAYS. Applied
# whether or not offsite sync is configured, so a dev setup with no
# S3 bucket doesn't fill the volume forever.
find "${BACKUP_DIR}" -name "*.dump" -mtime "+${RETENTION_DAYS}" -print -delete

# Optional offsite copy (S3-compatible: AWS S3, MinIO, R2, etc). Skipped
# entirely if BACKUP_S3_BUCKET is unset -- local-only is the safe
# default for a dev docker-compose stack, but production MUST set this
# (a backup on the same disk as the primary is not a backup against
# disk/server failure -- see docs/RELIABILITY.md "Disaster recovery").
if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  if command -v aws > /dev/null 2>&1; then
    aws ${BACKUP_S3_ENDPOINT:+--endpoint-url "${BACKUP_S3_ENDPOINT}"} \
      s3 cp "${DUMP_FILE}" "s3://${BACKUP_S3_BUCKET}/logical/$(basename "${DUMP_FILE}")"
    echo "[pg_backup] synced to s3://${BACKUP_S3_BUCKET}/logical/$(basename "${DUMP_FILE}")"
  else
    echo "[pg_backup] BACKUP_S3_BUCKET is set but the aws CLI is not installed in this image -- see ops/backup/README.md" >&2
    write_metric 0
    exit 1
  fi
else
  echo "[pg_backup] BACKUP_S3_BUCKET not set -- local-only backup (fine for dev, NOT fine for production)"
fi

write_metric 1
echo "[pg_backup] done"
