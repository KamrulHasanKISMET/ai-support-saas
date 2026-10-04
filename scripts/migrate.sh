#!/usr/bin/env bash
# Apply db/init/*.sql to an EXISTING database (db/init only auto-runs on a
# fresh volume). PENDING_WORK.md C1a / C7 / C8: migrations 018, 019, 020, 021, 024, 025, 026.
#
#   scripts/migrate.sh              # apply 016+ (all are idempotent: IF NOT EXISTS)
#   scripts/migrate.sh 018 019 020  # apply only these prefixes
#
# Runs inside the compose postgres container. Stops at the first error.
# Older migrations (001-015) are NOT re-run by default: several are plain
# CREATE TABLE and would fail on a database that already has them.
set -euo pipefail
cd "$(dirname "$0")/.."
PREFIXES=("$@"); [ ${#PREFIXES[@]} -eq 0 ] && PREFIXES=(016 017 018 019 020 021 022 023 024 025 026)
DB_USER="${POSTGRES_USER:-saas_user}"; DB_NAME="${POSTGRES_DB:-ai_support_saas}"
for p in "${PREFIXES[@]}"; do
  f=$(ls db/init/${p}_*.sql 2>/dev/null | head -1 || true)
  [ -z "$f" ] && { echo "no migration with prefix $p" >&2; exit 1; }
  echo "==> applying $f"
  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" < "$f"
done
echo "done."
