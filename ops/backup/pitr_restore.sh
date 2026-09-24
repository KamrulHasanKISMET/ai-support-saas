#!/usr/bin/env bash
# Point-in-time recovery restore, into a scratch/NON-PRODUCTION
# container. Run on the HOST (needs the `docker` CLI). Restores the
# most recent base backup (or one you name) and replays archived WAL
# up to either "as far as we have" or a specific timestamp you choose,
# then leaves a running scratch Postgres you can inspect/query before
# deciding to promote it to a real environment (see docs/RELIABILITY.md
# "Disaster recovery" for what "promote" means -- this script only gets
# you a verified, queryable restored database; it does NOT repoint the
# app's DATABASE_URL at it).
#
# Usage:
#   ./ops/backup/pitr_restore.sh                                  # replay all available WAL
#   ./ops/backup/pitr_restore.sh --target-time "2026-09-15 10:00:00+00"
#   ./ops/backup/pitr_restore.sh --base-backup 20260915T020000Z    # pick a specific base backup
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_BACKUP_ROOT="${BASE_BACKUP_ROOT:-${SCRIPT_DIR}/../../backups/base}"
WAL_ARCHIVE_DIR="${WAL_ARCHIVE_DIR:-${SCRIPT_DIR}/../../backups/wal_archive}"
SCRATCH_CONTAINER="saas_pitr_restore_$$"
SCRATCH_PORT="${PITR_RESTORE_PORT:-55433}"
TARGET_TIME=""
BASE_BACKUP_NAME=""

while [ $# -gt 0 ]; do
  case "$1" in
    --target-time) TARGET_TIME="$2"; shift 2 ;;
    --base-backup) BASE_BACKUP_NAME="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ -z "${BASE_BACKUP_NAME}" ]; then
  BASE_BACKUP_NAME="$(ls -t "${BASE_BACKUP_ROOT}" 2>/dev/null | head -1 || true)"
fi
if [ -z "${BASE_BACKUP_NAME}" ] || [ ! -d "${BASE_BACKUP_ROOT}/${BASE_BACKUP_NAME}" ]; then
  echo "No base backup found under ${BASE_BACKUP_ROOT}. Pass one: --base-backup <name>" >&2
  exit 1
fi

BASE_BACKUP_DIR="${BASE_BACKUP_ROOT}/${BASE_BACKUP_NAME}"
echo "[pitr_restore] base backup: ${BASE_BACKUP_DIR}"
echo "[pitr_restore] WAL archive: ${WAL_ARCHIVE_DIR}"
[ -n "${TARGET_TIME}" ] && echo "[pitr_restore] target time: ${TARGET_TIME}" || echo "[pitr_restore] target time: not set -- replaying all available WAL"

cleanup() {
  echo "[pitr_restore] (leaving ${SCRATCH_CONTAINER} running for inspection -- 'docker rm -f ${SCRATCH_CONTAINER}' when done)"
}
trap cleanup EXIT

WORKDIR="$(mktemp -d)"
echo "[pitr_restore] staging restored data dir at ${WORKDIR}/pgdata"
cp -a "${BASE_BACKUP_DIR}" "${WORKDIR}/pgdata"
chmod 700 "${WORKDIR}/pgdata"

# Recovery configuration (PostgreSQL 12+: recovery.signal + settings in
# postgresql.auto.conf, not a separate recovery.conf). `restore_command`
# pulls each WAL segment Postgres asks for out of the archive volume as
# it replays -- this is the actual "point-in-time recovery" mechanism,
# not just "start a Postgres pointed at old files".
touch "${WORKDIR}/pgdata/recovery.signal"
{
  echo "restore_command = 'cp /wal_archive/%f %p'"
  if [ -n "${TARGET_TIME}" ]; then
    echo "recovery_target_time = '${TARGET_TIME}'"
    echo "recovery_target_action = 'promote'"
  fi
} >> "${WORKDIR}/pgdata/postgresql.auto.conf"

echo "[pitr_restore] starting scratch Postgres ${SCRATCH_CONTAINER} on port ${SCRATCH_PORT} in recovery mode"
docker run -d --name "${SCRATCH_CONTAINER}" \
  -v "${WORKDIR}/pgdata:/var/lib/postgresql/data" \
  -v "${WAL_ARCHIVE_DIR}:/wal_archive:ro" \
  -p "${SCRATCH_PORT}:5432" \
  pgvector/pgvector:pg16 > /dev/null

echo "[pitr_restore] replaying WAL (this can take a while depending on how much WAL there is)..."
echo "[pitr_restore] tail logs with: docker logs -f ${SCRATCH_CONTAINER}"
echo "[pitr_restore] recovery is done once the container's logs show 'database system is ready to accept connections'"
echo "[pitr_restore] then verify with, e.g.: docker exec -it ${SCRATCH_CONTAINER} psql -U saas_user -d ai_support_saas -c 'select count(*) from tenants;'"
echo "[pitr_restore] NOTE: this scratch container is NOT wired to any app -- it exists for verification/manual recovery only."
