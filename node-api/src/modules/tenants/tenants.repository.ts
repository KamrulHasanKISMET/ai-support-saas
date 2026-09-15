import { pool } from "../../config/database";
import { generateApiKey } from "../../utils/crypto";

export interface Tenant {
  id: number;
  name: string;
  slug: string;
  plan: string;
  is_active: boolean;
  api_key: string;
  created_at: string;
}

export const tenantsRepository = {
  async create(name: string, slug: string): Promise<Tenant> {
    const apiKey = generateApiKey();
    const { rows } = await pool.query<Tenant>(
      `INSERT INTO tenants (name, slug, api_key) VALUES ($1, $2, $3) RETURNING *`,
      [name, slug, apiKey]
    );
    return rows[0];
  },

  async findBySlug(slug: string): Promise<Tenant | null> {
    const { rows } = await pool.query<Tenant>(
      `SELECT * FROM tenants WHERE slug = $1`,
      [slug]
    );
    return rows[0] ?? null;
  },

  async findById(id: number): Promise<Tenant | null> {
    const { rows } = await pool.query<Tenant>(
      `SELECT * FROM tenants WHERE id = $1`,
      [id]
    );
    return rows[0] ?? null;
  },
};
