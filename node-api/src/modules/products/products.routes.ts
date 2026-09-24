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
    const categoryId = req.query.categoryId ? Number(req.query.categoryId) : undefined;
    // Cache key includes the filter -- a plain GET / and a filtered
    // GET /?categoryId=5 must never share a cache entry.
    const cacheKey = categoryId
      ? `cache:products:${req.tenantId}:category:${categoryId}`
      : `cache:products:${req.tenantId}`;

    const cached = await redis.get(cacheKey);
    if (cached) {
      res.setHeader("X-Cache", "HIT");
      return res.json(JSON.parse(cached));
    }

    const { rows } = categoryId
      ? await pool.query(
          `SELECT * FROM products WHERE tenant_id = $1 AND category_id = $2 ORDER BY created_at DESC`,
          [req.tenantId, categoryId]
        )
      : await pool.query(
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
    const { sku, name, price, stock, categoryId } = req.body;
    const { rows } = await pool.query(
      `INSERT INTO products (tenant_id, sku, name, price, stock, category_id)
       VALUES ($1, $2, $3, $4, $5, $6) RETURNING *`,
      [req.tenantId, sku ?? null, name, price, stock ?? 0, categoryId ?? null]
    );
    // Invalidate every cached list for this tenant (plain + all
    // per-category variants) -- a new product could belong to any of
    // them, and there's no registry of which per-category keys exist
    // to invalidate selectively.
    const keys = await redis.keys(`cache:products:${req.tenantId}*`);
    if (keys.length) await redis.del(...keys);
    res.status(201).json(rows[0]);
  } catch (err) {
    next(err);
  }
});
