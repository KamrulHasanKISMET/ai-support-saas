import { NextFunction, Request, Response } from "express";

export interface TenantRequest extends Request {
  tenantId?: number;
  userId?: number;
  role?: string;
  authType?: "api_key" | "jwt";
}

/**
 * @deprecated Superseded by requireApiKey / requireAuth in
 * middleware/auth.ts. This blindly trusted a client-supplied
 * x-tenant-id header with no verification — any caller could read any
 * tenant's data by guessing an id. Left here only as a reference of
 * what NOT to do; nothing in app.ts uses this anymore.
 */
export function resolveTenant(
  req: TenantRequest,
  res: Response,
  next: NextFunction
) {
  const header = req.header("x-tenant-id");
  const tenantId = header ? Number(header) : NaN;

  if (!header || Number.isNaN(tenantId)) {
    return res.status(400).json({
      error: "MISSING_TENANT",
      message: "A valid x-tenant-id header is required.",
    });
  }

  req.tenantId = tenantId;
  next();
}
