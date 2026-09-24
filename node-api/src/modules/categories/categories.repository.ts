import { pool } from "../../config/database";

export interface ProductCategory {
  id: number;
  tenant_id: number;
  name: string;
  slug: string;
  description: string | null;
  parent_id: number | null;
  created_at: string;
  updated_at: string;
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
}

export const categoriesRepository = {
  async create(
    tenantId: number,
    data: { name: string; slug?: string; description?: string; parentId?: number | null }
  ): Promise<ProductCategory> {
    const slug = data.slug ? slugify(data.slug) : slugify(data.name);
    const { rows } = await pool.query<ProductCategory>(
      `INSERT INTO product_categories (tenant_id, name, slug, description, parent_id)
       VALUES ($1, $2, $3, $4, $5) RETURNING *`,
      [tenantId, data.name, slug, data.description ?? null, data.parentId ?? null]
    );
    return rows[0];
  },

  async list(tenantId: number): Promise<ProductCategory[]> {
    const { rows } = await pool.query<ProductCategory>(
      `SELECT * FROM product_categories WHERE tenant_id = $1 ORDER BY name ASC`,
      [tenantId]
    );
    return rows;
  },

  async findById(tenantId: number, id: number): Promise<ProductCategory | null> {
    const { rows } = await pool.query<ProductCategory>(
      `SELECT * FROM product_categories WHERE tenant_id = $1 AND id = $2`,
      [tenantId, id]
    );
    return rows[0] ?? null;
  },

  async findBySlug(tenantId: number, slug: string): Promise<ProductCategory | null> {
    const { rows } = await pool.query<ProductCategory>(
      `SELECT * FROM product_categories WHERE tenant_id = $1 AND slug = $2`,
      [tenantId, slug]
    );
    return rows[0] ?? null;
  },

  async update(
    tenantId: number,
    id: number,
    data: { name?: string; slug?: string; description?: string; parentId?: number | null }
  ): Promise<ProductCategory | null> {
    const existing = await this.findById(tenantId, id);
    if (!existing) return null;

    const name = data.name ?? existing.name;
    const slug = data.slug ? slugify(data.slug) : existing.slug;
    const description = data.description !== undefined ? data.description : existing.description;
    const parentId = data.parentId !== undefined ? data.parentId : existing.parent_id;

    const { rows } = await pool.query<ProductCategory>(
      `UPDATE product_categories
          SET name = $3, slug = $4, description = $5, parent_id = $6, updated_at = now()
        WHERE tenant_id = $1 AND id = $2
        RETURNING *`,
      [tenantId, id, name, slug, description, parentId]
    );
    return rows[0] ?? null;
  },

  /** Returns true if a row was actually deleted (tenant-scoped). */
  async delete(tenantId: number, id: number): Promise<boolean> {
    const { rowCount } = await pool.query(
      `DELETE FROM product_categories WHERE tenant_id = $1 AND id = $2`,
      [tenantId, id]
    );
    return (rowCount ?? 0) > 0;
  },
};
