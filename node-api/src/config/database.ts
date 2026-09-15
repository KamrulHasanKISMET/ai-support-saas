import { Pool } from "pg";
import { env } from "./env";

// Single shared pool for the whole Node.js process.
// PostgreSQL remains the source of truth (see architecture doc section 8/32).
export const pool = new Pool({
  connectionString: env.databaseUrl,
});

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
