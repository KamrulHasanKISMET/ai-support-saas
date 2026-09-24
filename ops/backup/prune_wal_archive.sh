#!/usr/bin/env bash
# Prunes archived WAL segments that are no longer needed by ANY
# retained base backup. Naive time-based pruning of the WAL archive is
# unsafe on its own -- deleting a WAL segment that a still-retained
# base backup needs for replay breaks PITR for that backup. This
# instead prunes only WAL older than the OLDEST base backup currently
# on disk, which is always safe: nothing retained needs WAL from
# before its own starting point.
#
# Run after pg_base_backup.sh (see backup_loop.sh) so retention always
# reflects the base backups that survived this cycle's pruning.
set -euo pipefail

BASE_BACKUP_DIR="${BASE_BACKUP_DIR:-/backup/base}"
WAL_ARCHIVE_DIR="${WAL_ARCHIVE_DIR:-/backup/wal_archive}"

if [ ! -d "${BASE_BACKUP_DIR}" ] || [ -z "$(ls -A "${BASE_BACKUP_DIR}" 2>/dev/null)" ]; then
  echo "[prune_wal_archive] no base backups on disk yet -- skipping WAL prune (nothing safe to prune against)"
  exit 0
fi

# Oldest retained base backup, by directory mtime (each is a
# timestamp-named dir from pg_base_backup.sh).
OLDEST_BASE_BACKUP_DIR="$(find "${BASE_BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null | sort -n | head -1 | cut -d' ' -f2-)"

if [ -z "${OLDEST_BASE_BACKUP_DIR:-}" ]; then
  echo "[prune_wal_archive] could not determine oldest base backup -- skipping WAL prune"
  exit 0
fi

# pg_basebackup writes backup_label with "START WAL LOCATION" naming
# the first WAL segment that backup needs. Any archived segment that
# SORTS BEFORE that filename (WAL segment names are lexically ordered)
# is not needed by this or any newer backup.
START_WAL_FILE="$(grep -oE '[0-9A-F]{24}' "${OLDEST_BASE_BACKUP_DIR}/backup_label" 2>/dev/null | head -1 || true)"

if [ -z "${START_WAL_FILE}" ]; then
  echo "[prune_wal_archive] could not read start WAL segment from ${OLDEST_BASE_BACKUP_DIR}/backup_label -- skipping WAL prune"
  exit 0
fi

echo "[prune_wal_archive] oldest retained base backup needs WAL from ${START_WAL_FILE} onward -- pruning older segments"

PRUNED=0
for f in "${WAL_ARCHIVE_DIR}"/*; do
  base="$(basename "${f}")"
  # Only touch plain 24-hex-char WAL segment filenames, never
  # .backup/.history files or anything unexpected in that directory.
  if [[ "${base}" =~ ^[0-9A-F]{24}$ ]] && [[ "${base}" < "${START_WAL_FILE}" ]]; then
    rm -f "${f}"
    PRUNED=$((PRUNED + 1))
  fi
done

echo "[prune_wal_archive] pruned ${PRUNED} WAL segment(s)"
