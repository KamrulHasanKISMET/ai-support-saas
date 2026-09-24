import { Pool } from "pg";
import { env } from "./env";
import { client, registry } from "../observability/registry";

// Single shared pool for the whole Node.js process.
// PostgreSQL remains the source of truth (see architecture doc section 8/32).
//
// Production Reliability workstream: pool size and timeouts are now
// explicit and env-tunable (previously all pg defaults: max 10,
// unbounded connect wait) -- see docs/RELIABILITY.md "Scaling triggers"
// for how DB_POOL_MAX relates to Postgres's own max_connections, and
// PROJECT_STATUS.md for what changed.
//
// DB query latency, timed at the single choke point every repository
// already goes through (`pool.query`) rather than instrumenting each
// of the ~30 call sites individually. Errors are re-thrown after being
// timed/counted, so a failing query still shows up as a fast-failing
// observation with status="error", not silently dropped.
const dbQueryDuration = new client.Histogram({
  name: "db_query_duration_seconds",
  help: "PostgreSQL query duration in seconds, as observed by node-api's pool wrapper",
  labelNames: ["status"],
  buckets: [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
  registers: [registry],
});

/**
 * Wraps `target.query` in place to record `db_query_duration_seconds`,
 * then returns the same object. Pulled out as a standalone function
 * (rather than inlined at pool construction) so it can be unit-tested
 * against a plain fake `{ query }` object -- see database.test.ts --
 * without opening a real socket to Postgres.
 *
 * Typed loosely (`any` at the query-args/return boundary) deliberately:
 * `pg.Pool.query` is a heavily overloaded method (string vs
 * QueryConfig, callback vs Promise, generic row type), and this
 * wrapper's whole job is "pass args/return value through unchanged,
 * just time it" -- trying to preserve every overload here would add
 * real complexity for no behavioral benefit. Callers still get full
 * `pool.query<T>(...)` type-checking as before; only this internal
 * instrumentation shim is untyped.
 */
export function instrumentPoolQuery<P extends { query: (...args: any[]) => any }>(
  target: P
): P {
  const rawQuery = target.query.bind(target);
  (target as { query: unknown }).query = (...args: unknown[]) => {
    const start = process.hrtime.bigint();
    const result = rawQuery(...args);
    const record = (status: "ok" | "error") => {
      dbQueryDuration.observe({ status }, Number(process.hrtime.bigint() - start) / 1e9);
    };
    if (result instanceof Promise) {
      return result.then(
        (value: unknown) => {
          record("ok");
          return value;
        },
        (err: unknown) => {
          record("error");
          throw err;
        }
      );
    }
    // pg's callback-style overload isn't used anywhere in this codebase
    // (confirmed: every call site is `await pool.query(...)`), but if one
    // ever appears, pass it through untimed rather than breaking it.
    return result;
  };
  return target;
}

export const pool = instrumentPoolQuery(
  new Pool({
    connectionString: env.databaseUrl,
    max: env.dbPoolMax,
    idleTimeoutMillis: env.dbPoolIdleTimeoutMs,
    connectionTimeoutMillis: env.dbPoolConnectionTimeoutMs,
    statement_timeout: env.dbStatementTimeoutMs,
  })
);

pool.on("error", (err) => {
  // eslint-disable-next-line no-console
  console.error("Unexpected PostgreSQL pool error", err);
});

/**
 * Every tenant-scoped query MUST pass tenantId explicitly.
 * This is a thin wrapper to make that intent visible at call sites,
 * not a magic enforcement mechanism — enforcement is via always
 * including `WHERE tenant_id = $1` in repository queries.
 */
export async function queryForTenant<T = unknown>(
  tenantId: number,
  text: string,
  params: unknown[] = []
) {
  return pool.query<T>(text, [tenantId, ...params]);
}

/**
 * Cheapest possible liveness probe for Postgres: `SELECT 1` (no table
 * scan, matches "do not run expensive database queries"). Used by
 * GET /readiness only — never by request-serving code paths.
 *
 * Also reports the pg Pool's current size/idle/waiting counts — `pg`
 * already tracks these internally (`pool.totalCount`/`idleCount`/
 * `waitingCount`), so reading them costs nothing, no extra query.
 *
 * Preserved from the pre-Reliability implementation: the Production
 * Reliability workstream's own draft of this file dropped this
 * function in favor of Prometheus-only pool gauges, but GET /readiness
 * (health.routes.ts) depends on the richer available/error shape this
 * returns, so it stays -- Prometheus gauges (below, via metrics.ts)
 * are additive, not a replacement.
 */
export async function checkDatabaseHealth(): Promise<{
  available: boolean;
  error?: string;
  poolTotal?: number;
  poolIdle?: number;
  poolWaiting?: number;
}> {
  try {
    await pool.query("SELECT 1");
  } catch (err) {
    return {
      available: false,
      error: err instanceof Error ? err.message : String(err),
    };
  }
  return {
    available: true,
    poolTotal: pool.totalCount,
    poolIdle: pool.idleCount,
    poolWaiting: pool.waitingCount,
  };
}
