import Redis from "ioredis";
import { env } from "./env";

// Redis is NOT the source of truth — only cache, queues, rate limiting,
// temporary state, and distributed locks (see architecture doc section 9).
export const redis = new Redis(env.redisUrl);

redis.on("error", (err) => {
  // eslint-disable-next-line no-console
  console.error("Redis connection error", err);
});

/** Cheapest possible liveness probe for Redis: PING. Used by
 * GET /readiness only — never by request-serving code paths. */
export async function checkRedisHealth(): Promise<{ available: boolean; error?: string }> {
  try {
    const reply = await redis.ping();
    return { available: reply === "PONG" };
  } catch (err) {
    return {
      available: false,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}
