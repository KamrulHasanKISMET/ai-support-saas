import { Router } from "express";
import { checkDatabaseHealth } from "../../config/database";
import { checkRedisHealth } from "../../config/redis";
import { getInfraSnapshot } from "../../utils/metrics";
import { metricsHandler } from "../../middleware/metrics";

/**
 * GET /health — "is the process alive?"
 *
 * Deliberately makes NO dependency calls (no DB, no Redis) — matches
 * python-api's GET /health exactly. Public, no auth: this is what
 * Docker's own healthcheck (docker-compose.yml) polls, and container
 * orchestration tooling in general won't have any of our app-level
 * credentials (x-api-key, JWT) to send.
 */
export const healthRouter = Router();
healthRouter.get("/", (_req, res) => {
  res.json({ status: "ok", service: "node-api", process: getInfraSnapshot() });
});

/**
 * GET /readiness — "can this service currently serve requests?"
 *
 * Distinct from /health: a process can be alive while genuinely unable
 * to serve a request (e.g. Postgres unreachable). Checks BOTH
 * dependencies node-api actually has (unlike python-api, which has no
 * real Redis usage yet — see AI_CONTEXT.md — node-api genuinely uses
 * Redis for rate limiting and caching, so it belongs here).
 *
 * Public, no auth — same reasoning as /health: a load balancer or
 * container orchestrator polling this has no tenant/dashboard
 * credentials to present, and nothing secret is revealed by
 * "database: up/down, redis: up/down, some latency numbers".
 */
export const readinessRouter = Router();
readinessRouter.get("/", async (_req, res) => {
  const [database, redisResult] = await Promise.all([
    checkDatabaseHealth(),
    checkRedisHealth(),
  ]);
  const ready = database.available && redisResult.available;

  const body = {
    status: ready ? "ready" : "not_ready",
    service: "node-api",
    dependencies: { database, redis: redisResult },
    process: getInfraSnapshot(),
  };
  res.status(ready ? 200 : 503).json(body);
});

/**
 * GET /metrics — Prometheus text exposition format (Production
 * Reliability workstream), scraped by monitoring/prometheus.yml.
 *
 * Previously a plain JSON snapshot of the in-memory request counters,
 * mounted behind requireAuth (JWT). Prometheus has no way to present a
 * JWT on a scrape, so this is now unauthenticated in app.ts, matching
 * python-api's equivalent — see docs/RELIABILITY.md "Metrics security"
 * for why that's safe (internal docker network only, not published
 * publicly) and what a production deployment should add in front of
 * it (reverse-proxy allowlist). The underlying JSON counters
 * (utils/metrics.ts) aren't gone -- they still back the `process`
 * field on /health and /readiness above.
 */
export const metricsRouter = Router();
metricsRouter.get("/", metricsHandler);
