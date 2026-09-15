import { Router } from "express";
import { TenantRequest } from "../../middleware/tenant";
import { pool } from "../../config/database";
import { redis } from "../../config/redis";

export const productsRouter = Router();

const CACHE_TTL_SECONDS = 60;

// Minimal CRUD placeholder. This table also backs the future
// check_product_stock() Tool (architecture doc section 24).
// Cache-aside via Redis (section 9): product lists change rarely
// compared to how often a Kernel Tool or dashboard might read them.
productsRouter.get("/", async (req: TenantRequest, res, next) => {
  try {
    const cacheKey = `cache:products:${req.tenantId}`;

    const cached = await redis.get(cacheKey);
    if (cached) {
      res.setHeader("X-Cache", "HIT");
      return res.json(JSON.parse(cached));
    }

    const { rows } = await pool.query(
      `SELECT * FROM products WHERE tenant_id = $1 ORDER BY created_at DESC`,
      [req.tenantId]
    );

    await redis.set(cacheKey, JSON.stringify(rows), "EX", CACHE_TTL_SECONDS);
    res.setHeader("X-Cache", "MISS");
    res.json(rows);
  } catch (err) {
    next(err);
  }
});

productsRouter.post("/", async (req: TenantRequest, res, next) => {
  try {
    const { sku, name, price, stock } = req.body;
    const { rows } = await pool.query(
      `INSERT INTO products (tenant_id, sku, name, price, stock)
       VALUES ($1, $2, $3, $4, $5) RETURNING *`,
      [req.tenantId, sku ?? null, name, price, stock ?? 0]
    );
    // Invalidate the cached list — stale reads are worse than a cache miss.
    await redis.del(`cache:products:${req.tenantId}`);
    res.status(201).json(rows[0]);
  } catch (err) {
    next(err);
  }
});
