import { Router } from "express";
import { pool } from "../../config/database";
import { TenantRequest } from "../../middleware/tenant";
import { AppError } from "../../middleware/errorHandler";

export const billingRouter = Router();

const VALID_PLANS = ["trial", "starter", "growth", "scale"] as const;

billingRouter.get("/subscription", async (req: TenantRequest, res, next) => {
  try {
    const { rows } = await pool.query(
      `SELECT s.*, t.plan AS tenant_plan
         FROM tenants t
         LEFT JOIN subscriptions s ON s.tenant_id = t.id
        WHERE t.id = $1`,
      [req.tenantId]
    );
    res.json(
      rows[0] ?? { tenant_plan: "trial", status: "active", provider: null }
    );
  } catch (err) {
    next(err);
  }
});

/**
 * Records an upgrade/downgrade intent locally. This does NOT charge a
 * card or call a payment provider — there is no gateway wired up here.
 *
 * Real integration point (do this before relying on this in production):
 *   1. Create a checkout/payment session with your provider (Stripe,
 *      SSLCommerz, bKash, etc.) using their SDK/API.
 *   2. On the provider's webhook confirming payment, update this same
 *      `subscriptions` row (status, provider, provider_ref,
 *      current_period_end) — do NOT trust a client-side "success"
 *      redirect alone to grant a paid plan.
 */
billingRouter.post("/subscription", async (req: TenantRequest, res, next) => {
  try {
    const { plan } = req.body as { plan: string };
    if (!VALID_PLANS.includes(plan as (typeof VALID_PLANS)[number])) {
      throw new AppError(
        "VALIDATION_ERROR",
        `plan must be one of: ${VALID_PLANS.join(", ")}`
      );
    }

    await pool.query(
      `INSERT INTO subscriptions (tenant_id, plan, status, provider)
       VALUES ($1, $2, 'active', 'manual')
       ON CONFLICT (tenant_id)
       DO UPDATE SET plan = EXCLUDED.plan, updated_at = CURRENT_TIMESTAMP`,
      [req.tenantId, plan]
    );
    await pool.query(`UPDATE tenants SET plan = $1 WHERE id = $2`, [
      plan,
      req.tenantId,
    ]);

    res.json({
      status: "recorded",
      plan,
      note: "No payment was processed — no payment provider is connected yet.",
    });
  } catch (err) {
    next(err);
  }
});
