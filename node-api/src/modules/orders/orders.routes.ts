import { Router } from "express";
import { TenantRequest } from "../../middleware/tenant";
import { pool } from "../../config/database";

export const ordersRouter = Router();

// Minimal CRUD placeholder. This is also the target of the future
// create_order() Tool, which the Kernel calls only after business-rule
// validation and (if configured) human approval (architecture doc
// section 24/25/34) — never directly from the LLM.
ordersRouter.get("/:id", async (req: TenantRequest, res, next) => {
  try {
    const { rows } = await pool.query(
      `SELECT * FROM orders WHERE tenant_id = $1 AND id = $2`,
      [req.tenantId, req.params.id]
    );
    res.json(rows[0] ?? null);
  } catch (err) {
    next(err);
  }
});
