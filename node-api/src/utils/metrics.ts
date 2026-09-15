/**
 * METRICS FOUNDATION (Phase 1) — request counters + infra metrics.
 *
 * Mirrors python-api/app/core/metrics.py: deliberately NOT Prometheus/
 * StatsD format and NOT a new dependency (no `prom-client`). In-memory
 * counters + Node's built-in `process` object, exposed as plain JSON.
 * Swap this for a real metrics backend in a later phase without
 * changing where instrumentation happens — `recordRequest()` is the
 * one call site every consumer needs.
 *
 * Single-process assumption: node-api runs as one process (see
 * docker-compose.yml — no cluster/pm2 workers). If that ever changes,
 * these counters become PER-PROCESS, not global — flagged here so a
 * future change doesn't silently misinterpret them.
 */

const processStartedAt = Date.now();

interface RequestMetrics {
  totalCount: number;
  errorCount: number; // status >= 500
  statusCounts: Record<number, number>;
  totalDurationMs: number;
}

const metrics: RequestMetrics = {
  totalCount: 0,
  errorCount: 0,
  statusCounts: {},
  totalDurationMs: 0,
};

/** Called once per HTTP request by middleware/metrics.ts. */
export function recordRequest(statusCode: number, durationMs: number): void {
  metrics.totalCount += 1;
  if (statusCode >= 500) {
    metrics.errorCount += 1;
  }
  metrics.statusCounts[statusCode] = (metrics.statusCounts[statusCode] ?? 0) + 1;
  metrics.totalDurationMs += durationMs;
}

export function getRequestMetricsSnapshot() {
  const avgDurationMs =
    metrics.totalCount > 0
      ? Math.round((metrics.totalDurationMs / metrics.totalCount) * 100) / 100
      : 0;
  return {
    totalCount: metrics.totalCount,
    errorCount: metrics.errorCount,
    statusCounts: { ...metrics.statusCounts },
    avgDurationMs,
  };
}

/**
 * CPU/RAM/uptime — a foundation, not a monitoring platform.
 * `cpuTimeSeconds` is CUMULATIVE user+system CPU time consumed since
 * process start (from `process.cpuUsage()`, microseconds converted to
 * seconds) — NOT an instantaneous percentage. Computing true CPU%
 * needs sampling over an interval, out of scope for this phase.
 * Documented explicitly in docs/OBSERVABILITY.md so it isn't misread
 * as "current load".
 */
export function getInfraSnapshot() {
  const cpu = process.cpuUsage();
  const mem = process.memoryUsage();
  return {
    uptimeSeconds: Math.round((Date.now() - processStartedAt) / 100) / 10,
    cpuTimeSeconds: Math.round((cpu.user + cpu.system) / 1000) / 1000,
    memoryRssMb: Math.round((mem.rss / (1024 * 1024)) * 10) / 10,
  };
}
