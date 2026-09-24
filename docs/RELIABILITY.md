# docs/RELIABILITY.md — Production Reliability / Capacity / Backup / Disaster Recovery

Scope: this document covers monitoring, alerting, load testing,
backups, PITR, restore verification, RPO/RTO, disaster recovery, and
scaling triggers for the existing architecture (`docs/ARCHITECTURE.md`)
as-is. It does **not** cover Agent Run Trace / Kernel / RAG / Memory /
Tool tracing (`app/trace/`) — that's a separate workstream; see
`docs/AGENT.md`.

Everything here extends the existing stack (docker-compose, the two
services' existing `/health` conventions, existing test patterns) —
nothing was redesigned or replaced.

---

## 1. Capacity monitoring (CPU, RAM, disk, container health)

Started with `docker compose --profile monitoring up` (see
`docker-compose.yml`, `monitoring/prometheus.yml`) — kept out of the
default `docker compose up` so the base dev experience is unaffected.

| Signal | Source | Notes |
|---|---|---|
| Host CPU / RAM / disk | `node-exporter` | Standard Prometheus host exporter. Also hosts the textfile collector directory the backup scripts write success/failure metrics into (see §7). |
| Per-container CPU / RAM | `cadvisor` | On Docker Desktop (Mac/Windows) some container-level metrics are limited by the VM boundary — host-level numbers from `node-exporter` are unaffected. |
| Process CPU/RAM/event-loop (node-api) | `prom-client`'s `collectDefaultMetrics` (`observability/registry.ts`), prefix `nodeapi_` | Already wired before this workstream added anything else to it. |
| Process CPU/RAM (python-api) | not yet instrumented at the process level | `psutil`-based process metrics were left out to keep this workstream's footprint small — cadvisor's per-container numbers cover this service adequately for now. See "Remaining risks". |

## 2. Application performance (request count, error rate, P50/P95/P99)

Both services now expose `GET /metrics` in Prometheus text format:

- **node-api**: `middleware/metrics.ts` — `http_requests_total`,
  `http_errors_total`, `http_request_duration_seconds` (histogram),
  labeled `method`/`route`/`status_code`. Route label is the matched
  Express route pattern (e.g. `/customers/:id`), not the raw path, to
  keep label cardinality bounded.
- **python-api**: `app/core/metrics_middleware.py` — the same three
  series, same label shape, read from Starlette's matched
  `request.scope["route"]`.

P50/P95/P99 come from `histogram_quantile()` over
`http_request_duration_seconds_bucket` in Prometheus/Grafana — see the
example queries in `monitoring/alerts.yml`
(`HighLatencyP95Warning`/`Critical`).

## 3. Database monitoring (connections, pool exhaustion, latency, locks)

| Signal | Source |
|---|---|
| node-api pool state | `db_pool_total_connections`, `db_pool_idle_connections`, `db_pool_waiting_requests` (`middleware/metrics.ts`, read from `pg.Pool`'s own counters) |
| python-api pool state | `db_pool_size`, `db_pool_checked_out_connections`, `db_pool_overflow_connections` (`app/api/routes/metrics.py`, read from SQLAlchemy's engine pool) |
| DB query latency (both services) | `db_query_duration_seconds` histogram, labeled `status="ok"/"error"`. node-api: timed at the single choke point every repository already goes through, `pool.query` (`config/database.ts`'s `instrumentPoolQuery`). python-api: timed via SQLAlchemy's `before_cursor_execute`/`after_cursor_execute`/`handle_error` events on the engine (`app/core/database.py`) — no repository/route call site touched in either service. |
| Postgres-side connections, locks, replication | `postgres-exporter` (`pg_stat_activity_count`, `pg_locks_count`, `pg_settings_max_connections`, etc.) |
| Redis | `redis-exporter`, plus node-api's own `redis_up` gauge (ping check on every `/metrics` scrape) |

"Pool exhaustion" specifically: `db_pool_waiting_requests > 0` sustained
means requests are queued waiting for a connection — see the
`DbPoolExhaustionWarning` alert.

## 4. Health / readiness

| Endpoint | Meaning | Checks |
|---|---|---|
| `GET /health` (both services) | Liveness — "is the process up at all" | None. Never fails due to a slow dependency; an orchestrator should restart the process on failure here. Unchanged by this workstream. |
| `GET /health/ready` (both services) | Readiness — "can this instance serve traffic right now" | node-api: Postgres + Redis, 2s timeout each (pre-existing, `middleware/readiness.ts`). python-api: Postgres only, 2s timeout (new — python-api never talks to Redis directly; see `PROJECT_STATUS.md`). An orchestrator should stop routing to an instance that fails readiness WITHOUT restarting it. |

`docker-compose.yml` healthchecks now use these: `postgres`/`redis` use
their own native tools (unchanged), `node-api`/`python-api` use
`GET /health` via a one-line Node/Python script (no `curl`/`wget`
added to either image, keeping them at their current size).

## 5. Load testing

Tooling: `loadtest/` (k6 script + a runner that also samples `docker
stats` and each service's `/metrics` for DB pool state during the
run). See `loadtest/README.md` for full usage.

Two scenarios:
- `health` — raw HTTP-layer capacity (`GET /health`, no auth/DB
  writes/LLM calls). Staged 10 → 50 → 100 → 200 VUs by default.
- `messages` — the real authenticated path (`POST /messages` →
  Kernel → LLM). Staged 2 → 5 → 10 VUs by default (deliberately small
  — this drives real, billed LLM calls and is rate-limited by the
  provider, not just this stack).

**Results: not yet run.** This was built and reviewed for correctness
in an environment with no Docker daemon and no network access — it has
not been executed against a live deployment. Per this workstream's own
instructions, no capacity number is claimed here without having
actually run it. Action for whoever picks this up next:

```bash
docker compose up -d
./loadtest/run.sh                                    # scenario: health
# then, once you have a tenant api_key from POST /auth/signup:
TEST_API_KEY=... SCENARIO=messages ./loadtest/run.sh
```

...and replace this section with the actual throughput/P95/P99/error/
CPU/RAM/DB-connection numbers from `loadtest/results/<timestamp>/`.

## 6. Alerting

Rules: `monitoring/alerts.yml`, loaded by `prometheus.yml`, routed
through `alertmanager.yml` (started with the `monitoring` profile).

| Group | Conditions |
|---|---|
| `service_availability` | Any scraped service down 1m (critical); node-api reporting zero live DB connections for 2m as a proxy for readiness failure (critical) — see the rule's own comment for why this is a proxy rather than a direct `/health/ready` probe. |
| `capacity` | Host CPU >75%/10m (warning), >90%/5m (critical); host RAM >80%/10m (warning), >92%/5m (critical); disk >75%/15m (warning), >90%/5m (critical); container memory >80% of its limit/10m (warning). |
| `application_performance` | P95 latency >1s/10m (warning), >3s/5m (critical); error rate >2%/10m (warning), >10%/5m (critical). |
| `database` | Pool requests queued (warning); overflow connections in use — python-api (warning); Postgres connections >80%/5m (warning), >95%/2m (critical); DB query P95 >500ms/10m (warning); >20 non-shared locks held/5m (warning). |
| `backups` | Last backup (logical or physical) failed (critical, fires immediately); no successful logical or physical backup in >25h (critical — catches the backup container itself being down, not just an in-band failure). |

**Notification channel is not wired.** `monitoring/alertmanager.yml`
ships a working route/grouping structure but points at no real
receiver (Slack/email/PagerDuty all commented out, placeholder
values) — that needs credentials only the deploying team has. Until
filled in, firing alerts are visible in the Prometheus
(`:9090/alerts`) and Alertmanager (`:9093`) UIs but not pushed
anywhere. See `monitoring/alertmanager.yml`'s comments for how to fill
this in.

The specific thresholds above are reasonable starting points for this
app's shape (chat-style request/response, single Postgres instance),
not numbers derived from the load test in §5 (which hasn't been run
yet) — revisit them once real traffic or load-test data exists.

## 7. PostgreSQL backup

`pg_backup` service (`docker-compose.yml`, part of the **default**
stack — not gated behind a profile, since backups are the point of
this workstream) runs `ops/backup/backup_loop.sh` on a loop, every
`BACKUP_INTERVAL_HOURS` (default 24h):

1. **`pg_backup.sh`** — logical backup: `pg_dump -Fc` (custom format —
   compressed, supports selective/parallel restore via `pg_restore`).
   Self-checked with `pg_restore --list` immediately after (catches a
   truncated/corrupt file, e.g. disk-full-mid-write, right away rather
   than at restore time during a real incident).
2. **`pg_base_backup.sh`** — physical backup: `pg_basebackup`, the
   basis for PITR (§8).
3. Both prune local copies past `BACKUP_RETENTION_DAYS` (default 7),
   and sync to S3-compatible storage if `BACKUP_S3_BUCKET` is set
   (unset = local-only, the safe default for dev; **production must
   set this** — a backup on the same disk as the primary does not
   survive disk/server failure, see §11).
4. Both write a Prometheus textfile-collector metric
   (`pg_backup_last_success`, `pg_base_backup_last_success`,
   `*_last_run_timestamp_seconds`) — this is what the `backups` alert
   group (§6) watches.

Details, env vars, and file layout: `ops/backup/README.md`.

## 8. PITR (point-in-time recovery)

Enabled via continuous WAL archiving on the `postgres` service
(`docker-compose.yml`): `wal_level=replica`, `archive_mode=on`,
`archive_command` copying each filled WAL segment into the shared
`wal_archive` volume, `archive_timeout=300` (bounds how much
unarchived WAL — and therefore how much data — a crash between backups
could lose, independent of write volume; see §10).

`ops/backup/prune_wal_archive.sh` (run after each base backup in the
loop) prunes archived WAL that's older than the **oldest currently
retained** base backup — never anything a still-retained backup could
need for replay, computed from that backup's own `backup_label`.

Recovery mechanism: restore a physical base backup, then let Postgres
replay archived WAL forward from that point, either to "as far as the
archive goes" or to a specific `recovery_target_time`. Automated for a
non-production scratch environment by `ops/backup/pitr_restore.sh` —
see §9.

## 9. Restore (reproducible, non-production)

Two scripts, both run on the **host** (need the `docker` CLI directly,
not just the `pg_backup` container), both operate on **throwaway**
containers and never touch the real `saas_postgres` container/data:

- **`ops/backup/restore_drill.sh`** — restores the newest (or a named)
  logical dump into a scratch Postgres container, verifies the core
  tables (`tenants`/`customers`/`conversations`/`messages`) exist,
  tears the container down. This is the quick, cheap, "is our backup
  actually restorable" check — run it on a schedule (weekly is a
  reasonable starting cadence), not just once.
- **`ops/backup/pitr_restore.sh`** — restores a physical base backup
  and replays WAL (optionally to a specific `--target-time`) into a
  scratch container left running for inspection/verification. This is
  the drill for the PITR/disaster-recovery path specifically (§11).

Full usage in `ops/backup/README.md`.

## 10. RPO / RTO

**These are targets based on what's actually implemented (§7–§9), not
measurements from an incident or a rehearsed timed drill** — the
honest next step is to run `restore_drill.sh`/`pitr_restore.sh` a few
times, time them for real, and correct these numbers accordingly.

| | Target | Basis |
|---|---|---|
| **RPO (data loss)** | ≤ 5 minutes | `archive_timeout=300` forces a WAL segment archive at least every 5 minutes even during low write volume; under load, segments fill and archive faster than that. Worst case (the primary disk itself is destroyed the instant before an in-flight segment would have archived) loses up to that ~5 minutes of committed-but-not-yet-archived writes. |
| **RTO (time to restore)** | Non-production verification restore: well under 1 hour (a `pg_dump`/`pg_basebackup` of this schema at current data volumes is small; restoring one is mostly fixed container-startup overhead — see §9's scripts for the actual mechanics) | Untimed estimate — no drill has been run and timed yet. |
| **RTO (production cutover after a real disaster)** | Not yet targetable with a real number | Depends on decisions this document flags but doesn't resolve — see §11's "what this runbook does NOT cover". |

Once `restore_drill.sh`/`pitr_restore.sh` have been run for real,
replace the RTO row with actual wall-clock times from those runs.

## 11. Disaster recovery

Scenario: the server/disk running `saas_postgres` is gone (not "the
container crashed and docker-compose restarts it" — that's already
handled by `restart: unless-stopped` and needs no runbook) — a real
disk failure, host loss, or similar, and you're rebuilding on a new
environment.

1. **Provision** a new host with Docker + this repository checked out
   (or just this repo's `docker-compose.yml` + `ops/` + `db/init/` if
   standing up Postgres alone first).
2. **Retrieve the latest backups** from offsite storage
   (`BACKUP_S3_BUCKET` — this is why §7 says production must set it;
   if backups were local-only, they were destroyed along with the
   primary and this recovery path is unavailable).
3. **Restore**, choosing based on how fresh you need to be:
   - Latest logical dump (`pg_backup.sh`'s output) via `pg_restore` —
     fastest, loses up to `BACKUP_INTERVAL_HOURS` of data (default up
     to 24h since the last successful dump).
   - Base backup + WAL replay via `ops/backup/pitr_restore.sh` (adjust
     its `BASE_BACKUP_ROOT`/`WAL_ARCHIVE_DIR` to point at the
     retrieved-from-S3 copies instead of the local volumes it defaults
     to) — recovers to within the RPO in §10.
4. **Verify** the restored database with the same checks
   `restore_drill.sh` runs (core tables present, row counts sane)
   before pointing anything at it.
5. **Re-point the application**: update `DATABASE_URL` in both
   services' `.env` (or the deployment's equivalent secret) to the new
   Postgres, restart `node-api`/`python-api`.
6. **Re-establish WAL archiving and the backup loop** on the new
   primary immediately (the `docker-compose.yml` config does this
   automatically once `pg_backup`/`postgres` are back up against the
   new volume) — until then, the new primary itself is unprotected.

### What this runbook does NOT cover

Being direct about the gaps rather than implying more coverage than
exists:

- **Automated failover.** This is a single Postgres instance with no
  standby/replica — recovery here is "restore from backup onto a new
  instance," not "promote a hot standby." There is no automatic
  detection-and-cutover; a human runs the steps above.
- **DNS/networking re-pointing** for a real production deployment
  (load balancer targets, DNS records) — this repo's docker-compose
  setup doesn't have those in the first place; whatever deployment
  target replaces it (not decided anywhere in this repo yet — see
  `docs/ROADMAP.md`) will need its own runbook addendum here.
- **node-api/python-api application-server recovery** — both are
  stateless (all state is in Postgres/Redis), so "disaster recovery"
  for them is just "redeploy the container image against the restored
  database," already covered by step 5 above. Redis is cache/rate-limit/
  queue state only (`docs/ARCHITECTURE.md` §9) — losing it loses no
  durable data, just warms up cold on restart.

## 12. Scaling triggers

What to watch (from §1–§3/§6) and what it implies:

| Bottleneck signal | Means | Do this |
|---|---|---|
| `db_pool_waiting_requests > 0` sustained (node-api) or `db_pool_overflow_connections` sustained (python-api), while Postgres's own `pg_stat_activity_count / pg_settings_max_connections` is still comfortably low | The **app-side pool** is the limit, not Postgres itself | Raise `DB_POOL_MAX` (node-api) / `DB_POOL_SIZE`+`DB_MAX_OVERFLOW` (python-api) — cheap, no new infrastructure. Keep the sum of all replicas' max pool sizes under Postgres's `max_connections`. |
| `pg_stat_activity_count / pg_settings_max_connections` itself approaching 80–95% even with app pools unchanged | **Postgres connection ceiling**, not app-side | Either raise Postgres's `max_connections` (bounded by available RAM — each connection has real memory overhead) or add a connection pooler (e.g. PgBouncer) in front of it — a genuinely new piece of infrastructure, not to be added speculatively before this signal shows up. |
| `db_query_duration_seconds` P95 climbing with connection counts flat/low | Query/schema/index problem, or the instance is undersized for the data volume, not a concurrency problem | Profile the slow queries (`pg_stat_statements`, not yet enabled — see "Remaining risks") before assuming more hardware fixes it. |
| Host/container CPU or RAM sustained near the `capacity` alert thresholds (§6) on `node-api`/`python-api`, while DB signals above are healthy | The **application process** is the bottleneck | node-api and python-api are both stateless — add replicas behind a load balancer (not present in this docker-compose setup; a real deployment target decision, see `docs/ROADMAP.md`) before considering bigger single instances. |
| Host disk usage climbing toward the `capacity` alert thresholds (§6) | Data volume growth (Postgres data dir) or backup accumulation | Check `BACKUP_RETENTION_DAYS` first (cheap fix) before assuming the Postgres volume itself needs to grow. |
| `HighLatencyP95`/`HighErrorRate` firing specifically on `POST /ai/kernel/run` / `POST /messages`, with DB/CPU/RAM signals healthy | The bottleneck is the **LLM provider call**, not this stack | Out of scope for infrastructure scaling — see the LLM provider's own rate limits/latency, and `docs/KERNEL.md`. |

None of the specific thresholds above (e.g. "raise `DB_POOL_MAX` to
X") are prescribed with concrete replacement numbers — that requires
the load test in §5 to have actually been run first.

---

## Remaining risks / explicitly out of scope

- **Load test not yet executed** (§5) — the single biggest gap. Every
  number in §10/§12 is a reasoned estimate, not a measurement.
- **RPO/RTO not yet drilled with a stopwatch** (§10).
- **Alertmanager has no real receiver configured** (§6) — alerts are
  visible in-UI only until Slack/email/PagerDuty/etc is wired in.
- **`ReadinessFailing` alert is a proxy**, not a direct
  `GET /health/ready` probe (see the rule's comment in
  `monitoring/alerts.yml`) — adding `blackbox_exporter` for a real
  synthetic probe is a reasonable, small follow-up.
- **`pg_stat_statements` is not enabled** — would sharpen "which query
  is slow" beyond the aggregate `db_query_duration_seconds` histograms
  this workstream added. Left out to keep this change's footprint
  small; enabling it is a one-line addition to `db/init/`
  (`CREATE EXTENSION pg_stat_statements`) plus a Postgres restart with
  `shared_preload_libraries` set, whenever it's prioritized.
- **python-api has no process-level CPU/RAM metric** of its own
  (unlike node-api's `collectDefaultMetrics`) — cadvisor's
  per-container numbers stand in for now; a `psutil`-based addition to
  `app/core/metrics.py` would close this gap if finer-grained,
  in-process attribution is needed later.
- **No automated failover / standby** — see §11 "What this runbook
  does NOT cover".
- **Replication user reuses the app superuser** for `pg_basebackup`
  (`ops/backup/pg_base_backup.sh`) — fine for this single-instance
  setup, but a dedicated least-privilege replication role should be
  created before this evolves toward a real standby/HA setup.
- **`aws` CLI is not preinstalled** in the `pg_backup` container image
  — offsite (S3) sync requires either switching that service's image
  or adding an install step; the scripts detect and fail loudly rather
  than silently skip it (see `ops/backup/README.md`).
- **This workstream did not touch, and does not claim any coverage
  of**, Agent Run Trace / Kernel / RAG / Memory / Tool tracing
  (`app/trace/`) — see `docs/AGENT.md` for that workstream's own
  status.
