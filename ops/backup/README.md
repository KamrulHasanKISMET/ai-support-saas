# ops/backup

Backup, PITR, and restore-verification tooling for the Production
Reliability workstream. Full design/rationale, RPO/RTO targets, and the
disaster-recovery runbook are in **`docs/RELIABILITY.md`** — this file
just documents what each script does and how to run it.

## What runs automatically (inside docker-compose)

The `pg_backup` service (see `docker-compose.yml`) runs
`backup_loop.sh`, which every `BACKUP_INTERVAL_HOURS` (default 24):

1. `pg_backup.sh` — logical `pg_dump -Fc` of the whole database, an
   integrity check (`pg_restore --list`), retention pruning, and an
   optional sync to S3-compatible storage.
2. `pg_base_backup.sh` — physical `pg_basebackup`, the same
   retention/S3/metric pattern, needed for PITR.
3. `prune_wal_archive.sh` — prunes the continuously-archived WAL
   segments (written directly by Postgres's own `archive_command`, not
   by this container) down to only what the oldest retained base
   backup still needs.

Each script writes a Prometheus textfile-collector metric
(`pg_backup_last_success`, `pg_base_backup_last_success`) to the
`metrics_textfile` volume, scraped by `node_exporter` — see
`monitoring/alerts.yml` for the alert on backup failure.

## What you run by hand (on the HOST, not in a container)

### Restore drill (`restore_drill.sh`)

The reproducible, non-production restore/verification procedure
requested for this workstream. Spins up a **throwaway** Postgres
container, restores the newest (or a named) logical dump into it, runs
a few sanity checks, and tears the container down. Never touches the
real `saas_postgres` container.

```bash
./ops/backup/restore_drill.sh
./ops/backup/restore_drill.sh backups/dumps/ai_support_saas_20260915T020000Z.dump
```

Run this **on a schedule** (weekly is a reasonable starting cadence —
see docs/RELIABILITY.md), not just once, since an untested backup is
not a verified backup.

### Point-in-time recovery (`pitr_restore.sh`)

Restores a physical base backup and replays archived WAL, either as
far as the archive goes or up to a specific timestamp, into a scratch
container left running for inspection. This is the mechanism behind
the "restore to a specific point in time" and "recover from
server/disk failure" procedures in `docs/RELIABILITY.md`.

```bash
./ops/backup/pitr_restore.sh
./ops/backup/pitr_restore.sh --target-time "2026-09-15 10:00:00+00"
./ops/backup/pitr_restore.sh --base-backup 20260915T020000Z
```

The scratch container it starts is never wired to the app — promoting
a verified restore into an environment the app actually points at is
a deliberate, separate step (see docs/RELIABILITY.md "Disaster
recovery").

## Configuration (env vars, set in the root `.env` / docker-compose)

| Variable | Default | Notes |
|---|---|---|
| `BACKUP_INTERVAL_HOURS` | `24` | How often the loop runs both backup types. |
| `BACKUP_RETENTION_DAYS` | `7` | Local + S3 retention for both dumps and base backups. |
| `BACKUP_S3_BUCKET` | unset | If unset, backups are **local-only** — fine for dev, not for production (a backup on the same disk as the primary doesn't survive disk/server failure). |
| `BACKUP_S3_ENDPOINT` | unset | Set for a non-AWS S3-compatible provider (MinIO, R2, etc); leave unset for real AWS S3. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | unset | Credentials for the above. |

**The `pg_backup` container image does not include the `aws` CLI by
default.** If you set `BACKUP_S3_BUCKET`, either switch the
`pg_backup` service's image to one with it preinstalled, or add an
install step — the scripts detect its absence and fail loudly (with a
metric + non-zero exit) rather than silently skipping the offsite
copy.

## Local file layout

Inside the `pg_backup` container (and the named volumes backing them):

```
/backup/dumps/          -- logical pg_dump -Fc files (pg_backups volume)
/backup/base/<ts>/       -- physical pg_basebackup output (pg_backups volume)
/backup/wal_archive/     -- continuously archived WAL (wal_archive volume, shared with postgres)
/backup/metrics/*.prom   -- node_exporter textfile-collector metrics (metrics_textfile volume)
```
