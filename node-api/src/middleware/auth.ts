import { NextFunction, Response } from "express";
import jwt from "jsonwebtoken";
import { env } from "../config/env";
import { authRepository } from "../modules/auth/auth.repository";
import { AppError } from "./errorHandler";
import { TenantRequest } from "./tenant";

/**
 * For channel-facing / server-to-server routes (message intake,
 * customer creation from a webhook). Requires a real per-tenant secret
 * — the `x-api-key` header — looked up against tenants.api_key.
 *
 * This is what closes the gap the old resolveTenant left open: a
 * client can no longer just declare "I am tenant 7" via a plain header;
 * they must present that tenant's actual secret.
 */
export async function requireApiKey(
  req: TenantRequest,
  res: Response,
  next: NextFunction
) {
  try {
    const apiKey = req.header("x-api-key");
    if (!apiKey) {
      throw new AppError("MISSING_API_KEY", "An x-api-key header is required.", 401);
    }

    const tenant = await authRepository.findTenantByApiKey(apiKey);
    if (!tenant || !tenant.is_active) {
      throw new AppError("INVALID_API_KEY", "Invalid or inactive API key.", 401);
    }

    req.tenantId = tenant.id;
    req.authType = "api_key";
    next();
  } catch (err) {
    next(err);
  }
}

/**
 * For dashboard-facing routes (staff viewing/managing their own
 * tenant's data). Requires `Authorization: Bearer <jwt>` issued by
 * POST /auth/login or /auth/signup. tenantId/userId/role come from the
 * verified token claims — never from anything the client can set
 * directly, so one tenant's staff cannot address another tenant's data
 * by changing a header.
 */
export function requireAuth(
  req: TenantRequest,
  res: Response,
  next: NextFunction
) {
  try {
    const header = req.header("authorization");
    const token = header?.startsWith("Bearer ") ? header.slice(7) : null;
    if (!token) {
      throw new AppError("MISSING_TOKEN", "An Authorization: Bearer <token> header is required.", 401);
    }

    const claims = jwt.verify(token, env.jwtSecret) as {
      userId: number;
      tenantId: number;
      role: string;
    };

    req.tenantId = claims.tenantId;
    req.userId = claims.userId;
    req.role = claims.role;
    req.authType = "jwt";
    next();
  } catch {
    next(new AppError("INVALID_TOKEN", "Invalid or expired token.", 401));
  }
}

/**
 * For /customers only (app.ts): this single router serves TWO actors
 * that the rest of the codebase keeps on separate routers/paths --
 * OUR OWN channel adapters (e.g. the website widget, x-api-key,
 * `requireApiKey` above) creating/reading a customer record for the
 * person they're chatting with, AND staff on the dashboard (JWT,
 * `requireAuth` above) listing/searching/editing/deleting their
 * tenant's customers (CUSTOMER-KNOWLEDGE-RAG-API-001 phase 1).
 *
 * Every other router in this codebase needs exactly one of the two
 * (see conversations.routes.ts / channels.routes.ts comments for
 * "dashboard-facing" vs messages.routes.ts for "channel-facing") --
 * splitting /customers into two routers on the same base path doesn't
 * work with Express's middleware chain (a throwing first-in-chain
 * `requireApiKey` would reject a JWT-only dashboard request before a
 * second `app.use("/customers", requireAuth, ...)` ever ran). This
 * tries `x-api-key` first (cheaper, no crypto), falls back to a
 * Bearer JWT, and only 401s if NEITHER credential is present/valid --
 * it doesn't change what either credential means, just accepts both
 * on this one shared path. `req.authType` still tells a handler which
 * one was actually used, if that ever matters.
 */
export async function requireApiKeyOrAuth(
  req: TenantRequest,
  res: Response,
  next: NextFunction
) {
  if (req.header("x-api-key")) {
    return requireApiKey(req, res, next);
  }
  if (req.header("authorization")) {
    return requireAuth(req, res, next);
  }
  next(
    new AppError(
      "MISSING_CREDENTIALS",
      "An x-api-key header or Authorization: Bearer <token> header is required.",
      401
    )
  );
}
