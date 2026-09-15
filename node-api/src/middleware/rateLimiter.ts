import { NextFunction, Response } from "express";
import { redis } from "../config/redis";
import { AppError } from "./errorHandler";
import { TenantRequest } from "./tenant";

/**
 * Fixed-window rate limiter per tenant, backed by Redis (architecture
 * doc section 9 — Redis owns rate limiting; Postgres never sees this).
 *
 * Uses INCR + EXPIRE: the first request in a window creates the key
 * with a TTL, subsequent requests just increment. Cheap, no read-then-
 * write race beyond an acceptable off-by-one at the window boundary —
 * fine for an MVP limiter; swap for a sliding-window/token-bucket
 * script later if bursty traffic at window edges becomes a problem.
 */
export function rateLimit(limit: number, windowSeconds: number) {
  return async (req: TenantRequest, res: Response, next: NextFunction) => {
    try {
      if (!req.tenantId) {
        return next(); // tenant middleware runs first; nothing to key on yet
      }

      const key = `ratelimit:tenant:${req.tenantId}:${req.path}`;
      const count = await redis.incr(key);
      if (count === 1) {
        await redis.expire(key, windowSeconds);
      }

      res.setHeader("X-RateLimit-Limit", limit);
      res.setHeader("X-RateLimit-Remaining", Math.max(0, limit - count));

      if (count > limit) {
        throw new AppError(
          "RATE_LIMITED",
          `Too many requests. Limit is ${limit} per ${windowSeconds}s.`,
          429
        );
      }

      next();
    } catch (err) {
      next(err);
    }
  };
}
