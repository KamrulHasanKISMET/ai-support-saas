import { randomUUID } from "crypto";
import { NextFunction, Request, Response } from "express";

export interface RequestIdRequest extends Request {
  requestId: string;
}

/**
 * Assigns (or reuses, if a caller already set one) a request id and
 * echoes it back in the response header. This id is forwarded to the
 * Python AI Service on every Kernel call (see ai.client.ts) so a single
 * customer interaction can be traced across both services in the logs
 * (architecture doc section 33, Observability).
 */
export function requestId(req: Request, res: Response, next: NextFunction) {
  const incoming = req.header("x-request-id");
  const id = incoming && incoming.length > 0 ? incoming : randomUUID();
  (req as RequestIdRequest).requestId = id;
  res.setHeader("x-request-id", id);
  next();
}
