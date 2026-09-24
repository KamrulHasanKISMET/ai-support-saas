import client from "prom-client";
import { NextFunction, Request, Response } from "express";
import { recordRequest } from "../utils/metrics";
import { logger } from "../utils/logger";
import { RequestIdRequest } from "./requestId";
import { pool } from "../config/database";
import { redis } from "../config/redis";
import { registry } from "../observability/registry";

/**
 * Wraps every HTTP request node-api receives: times it, records it in
 * the in-memory counters (utils/metrics.ts), logs one structured line,
 * AND (Production Reliability workstream) records the same request
 * into Prometheus series scraped at GET /metrics. Mounted AFTER
 * requestId (app.ts) so req.requestId is already set.
 *
 * Deliberately ONE middleware doing both jobs, rather than two
 * middlewares stacked on every request — the Phase 1 JSON counters
 * (utils/metrics.ts, surfaced via /health and /readiness's `process`
 * field) and the Prometheus series below are different consumers of
 * the same underlying event, not competing implementations.
 *
 * Mirrors python-api's app/core/request_middleware.py (JSON/logging
 * side) and app/core/metrics.py (Prometheus side).
 */

export { registry };

const httpRequestDuration = new client.Histogram({
  name: "http_request_duration_seconds",
  help: "HTTP request duration in seconds, labeled by method/route/status_code",
  labelNames: ["method", "route", "status_code"],
  // Fine enough resolution at the low end to compute p50/p95/p99 for a
  // chat-style API where most requests should be well under 1s.
  buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
  registers: [registry],
});

const httpRequestsTotal = new client.Counter({
  name: "http_requests_total",
  help: "Total HTTP requests handled",
  labelNames: ["method", "route", "status_code"],
  registers: [registry],
});

const httpErrorsTotal = new client.Counter({
  name: "http_errors_total",
  help: "Total HTTP responses with status_code >= 400",
  labelNames: ["method", "route", "status_code"],
  registers: [registry],
});

// Postgres pool saturation — the leading indicator for "pool exhaustion"
// alerts (see monitoring/alerts.yml). `pg.Pool` tracks these counters in
// memory already; we just publish them.
const dbPoolTotal = new client.Gauge({
  name: "db_pool_total_connections",
  help: "Total PostgreSQL client connections currently held by the pool (in use + idle)",
  registers: [registry],
});
const dbPoolIdle = new client.Gauge({
  name: "db_pool_idle_connections",
  help: "Idle PostgreSQL client connections currently held by the pool",
  registers: [registry],
});
const dbPoolWaiting = new client.Gauge({
  name: "db_pool_waiting_requests",
  help: "Requests currently queued waiting for a PostgreSQL connection (>0 sustained = pool exhaustion)",
  registers: [registry],
});

// Redis connectivity as a simple up/down gauge — Redis has no pool to
// size here (ioredis manages a single multiplexed connection), so the
// useful signal is "can we reach it right now", checked on each scrape.
const redisUp = new client.Gauge({
  name: "redis_up",
  help: "1 if the last Redis PING succeeded, 0 otherwise",
  registers: [registry],
});

/**
 * Records method/route/status_code/duration for every request into
 * BOTH the legacy JSON counters (utils/metrics.ts) and the Prometheus
 * series above, and logs one structured line (mirrors
 * python-api's request_middleware.py). Route label for the Prometheus
 * series is read from `req.route` inside the `finish` handler (i.e.
 * after Express has matched the route), which keeps cardinality bounded
 * to the app's actual route patterns (e.g. `/customers/:id`) rather
 * than raw paths. Unmatched routes (404s) fall back to the literal path,
 * which is an accepted, bounded source of extra label values for a
 * service with a small number of real 404 sources.
 */
export function metricsMiddleware(req: Request, res: Response, next: NextFunction) {
  const startedAt = process.hrtime.bigint();

  res.on("finish", () => {
    const elapsedNs = process.hrtime.bigint() - startedAt;
    const durationMs = Number(elapsedNs) / 1_000_000;

    // Legacy JSON counters + structured log line (unchanged behavior).
    recordRequest(res.statusCode, durationMs);
    logger.info("http_request", {
      requestId: (req as RequestIdRequest).requestId,
      method: req.method,
      path: req.path,
      status: res.statusCode,
      latencyMs: Math.round(durationMs * 10) / 10,
    });

    // Prometheus series (Production Reliability workstream).
    const routeSuffix = (req as unknown as { route?: { path?: string } }).route?.path;
    const route = routeSuffix ? `${req.baseUrl}${routeSuffix}` : req.path;
    const labels = {
      method: req.method,
      route,
      status_code: String(res.statusCode),
    };
    httpRequestDuration.observe(labels, Number(elapsedNs) / 1e9);
    httpRequestsTotal.inc(labels);
    if (res.statusCode >= 400) {
      httpErrorsTotal.inc(labels);
    }
  });

  next();
}

/**
 * GET /metrics handler — refreshes point-in-time gauges, then serves
 * the registry in Prometheus text exposition format.
 *
 * Intentionally unauthenticated (see app.ts) so Prometheus can scrape
 * it over the internal docker-compose network without a JWT it has no
 * way to obtain — matches python-api's equivalent. Do not publish this
 * port to a public network without an allowlist/reverse-proxy rule in
 * front of it; see docs/RELIABILITY.md "Metrics security".
 */
export async function metricsHandler(_req: Request, res: Response) {
  dbPoolTotal.set(pool.totalCount);
  dbPoolIdle.set(pool.idleCount);
  dbPoolWaiting.set(pool.waitingCount);

  try {
    await redis.ping();
    redisUp.set(1);
  } catch {
    redisUp.set(0);
  }

  res.set("Content-Type", registry.contentType);
  res.end(await registry.metrics());
}
