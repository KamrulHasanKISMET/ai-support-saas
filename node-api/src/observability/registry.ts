import client from "prom-client";

/**
 * Production Reliability workstream.
 *
 * Single shared prom-client Registry for node-api, pulled out into its
 * own leaf module (no imports of its own) so that both `config/database.ts`
 * (DB query latency) and `middleware/metrics.ts` (HTTP latency, DB pool
 * gauges) can register metrics into it without importing each other --
 * database.ts needs to time queries at the one place they all flow
 * through (`pool.query`), and metrics.ts needs to read pool state, so a
 * database.ts <-> metrics.ts import would be circular.
 *
 * Deliberately separate from Agent Run Trace / Kernel tracing
 * (python-api `app/trace/`) -- that's per-request AI observability for
 * a different workstream. This is infra-level "is the service healthy
 * and how loaded is it" observability. See docs/RELIABILITY.md.
 */
export const registry = new client.Registry();

// process_cpu_*, process_resident_memory_bytes, nodejs_eventloop_lag_seconds,
// nodejs_heap_size_*, etc. -- the "RAM/CPU where practical" signal for a
// Node process that isn't behind a container-level exporter.
client.collectDefaultMetrics({ register: registry, prefix: "nodeapi_" });

export { client };
