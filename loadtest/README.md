# loadtest

Reproducible load testing for the Production Reliability workstream.
Uses [k6](https://k6.io/) (single binary, scriptable, gives
throughput/P50/P95/P99/error-rate directly) plus a small wrapper that
samples `docker stats` and each service's `/metrics` endpoint (DB pool
state) alongside it.

## Status

**Not yet run against a live deployment.** This tooling was written and
reviewed for correctness but not executed, because the environment
this was built in has no Docker daemon and no network access to
install k6 or bring the stack up. Run it in a real environment and
paste the results into `docs/RELIABILITY.md`'s "Load testing" section
— don't treat any number currently in that doc as measured until then.

## Prerequisites

- The stack running: `docker compose up -d` (or `up -d --profile
  monitoring` if you also want to watch Prometheus/Grafana during the
  run).
- [k6](https://k6.io/docs/get-started/installation/) installed, or use
  the Docker image: `docker run --rm -i --network host grafana/k6 run
  - < loadtest/script.js`.

## Running

```bash
# Baseline: raw HTTP-layer capacity, no auth, no DB writes, no LLM calls
./loadtest/run.sh

# Full end-to-end path (POST /messages -> Kernel -> LLM). Costs real
# API credits and is rate-limited by your LLM provider, not just this
# stack -- start with the small default stages (script.js) before
# raising VUS.
TEST_API_KEY=<a tenant api_key from POST /auth/signup> SCENARIO=messages ./loadtest/run.sh
```

Results land in `loadtest/results/<timestamp>/`:

- `k6_output.txt` / `k6_summary.json` — throughput, P50/P95/P99
  latency, error rate (from k6 itself).
- `docker_stats.log` — CPU/RAM per container, sampled every 5s during
  the run.
- `node_api_metrics_samples.log` / `python_api_metrics_samples.log` —
  DB pool state (`db_pool_*`) and request counters, sampled every 5s.

## Interpreting a run

1. Start with `SCENARIO=health` to find where raw HTTP handling starts
   degrading (event loop saturation, connection limits) — this is the
   ceiling before the database or LLM enter the picture at all.
2. Move to `SCENARIO=messages` at modest concurrency to see where the
   DB pool (`db_pool_waiting_requests > 0` in the samples) or DB query
   latency (`db_query_duration_seconds`) becomes the bottleneck instead
   — this is usually well below the `health` ceiling.
3. Whichever bottleneck you hit first is the one to fix/scale first.
   See `docs/RELIABILITY.md` "Scaling triggers" for what to change for
   each bottleneck (raise `DB_POOL_MAX`/`DB_POOL_SIZE` vs. raise
   Postgres's `max_connections` vs. add a node-api replica vs. move
   Postgres to bigger hardware).

Do not report a "this system handles N req/s" number from a single run
on a laptop as a production capacity figure — run it against
infrastructure sized like production, more than once, and treat the
`docs/RELIABILITY.md` numbers as a starting point to re-validate, not
a permanent fact.
