import { NextFunction, Request, Response } from "express";
import { classifyError } from "../utils/errorCategory";
import { logger } from "../utils/logger";
import { RequestIdRequest } from "./requestId";

export class AppError extends Error {
  statusCode: number;
  code: string;

  constructor(code: string, message: string, statusCode = 400) {
    super(message);
    this.code = code;
    this.statusCode = statusCode;
  }
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function errorHandler(
  err: unknown,
  req: Request,
  res: Response,
  next: NextFunction
) {
  const requestId = (req as RequestIdRequest).requestId;
  const category = classifyError(err);

  if (err instanceof AppError) {
    // 4xx AppErrors are expected/handled control flow (validation,
    // auth, not-found, ...) -- logged at info level with the category
    // for searchability, not as an alarming error.
    logger.info("request_error", {
      requestId,
      category,
      code: err.code,
      statusCode: err.statusCode,
      // AppError messages are already customer-safe by construction
      // (they're written to be returned in the response); never log
      // request bodies/headers here, which could contain secrets.
    });
    return res.status(err.statusCode).json({
      error: err.code,
      message: err.message,
    });
  }

  logger.error("unhandled_error", {
    requestId,
    category,
    error: err instanceof Error ? err.message : String(err),
  });
  return res.status(500).json({
    error: "INTERNAL_ERROR",
    message: "Something went wrong.",
  });
}
