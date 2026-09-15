import { NextFunction, Request, Response } from "express";
import { recordRequest } from "../utils/metrics";
import { logger } from "../utils/logger";
import { RequestIdRequest } from "./requestId";

/**
 * Wraps every HTTP request node-api receives: times it, records it in
 * the in-memory counters (utils/metrics.ts), logs one structured line.
 * Mounted AFTER requestId (app.ts) so req.requestId is already set.
 *
 * Mirrors python-api's app/core/request_middleware.py so both services
 * produce comparable request-level observability data.
 */
export function metricsMiddleware(req: Request, res: Response, next: NextFunction) {
  const startedAt = process.hrtime.bigint();

  res.on("finish", () => {
    const durationMs = Number(process.hrtime.bigint() - startedAt) / 1_000_000;
    recordRequest(res.statusCode, durationMs);
    logger.info("http_request", {
      requestId: (req as RequestIdRequest).requestId,
      method: req.method,
      path: req.path,
      status: res.statusCode,
      latencyMs: Math.round(durationMs * 10) / 10,
    });
  });

  next();
}
