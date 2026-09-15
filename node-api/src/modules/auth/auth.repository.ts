import { pool } from "../../config/database";

export interface UserRecord {
  id: number;
  tenant_id: number;
  email: string;
  password_hash: string;
  full_name: string | null;
  role: string;
}

export const authRepository = {
  async createUser(
    tenantId: number,
    email: string,
    passwordHash: string,
    fullName?: string,
    role = "owner"
  ): Promise<UserRecord> {
    const { rows } = await pool.query<UserRecord>(
      `INSERT INTO users (tenant_id, email, password_hash, full_name, role)
       VALUES ($1, $2, $3, $4, $5) RETURNING *`,
      [tenantId, email.toLowerCase(), passwordHash, fullName ?? null, role]
    );
    return rows[0];
  },

  async findUserByEmail(
    tenantSlug: string,
    email: string
  ): Promise<(UserRecord & { tenant_id: number }) | null> {
    const { rows } = await pool.query<UserRecord>(
      `SELECT u.* FROM users u
         JOIN tenants t ON t.id = u.tenant_id
        WHERE t.slug = $1 AND u.email = $2`,
      [tenantSlug, email.toLowerCase()]
    );
    return rows[0] ?? null;
  },

  async findTenantByApiKey(apiKey: string) {
    const { rows } = await pool.query(
      `SELECT id, name, slug, plan, is_active FROM tenants WHERE api_key = $1`,
      [apiKey]
    );
    return rows[0] ?? null;
  },
};
