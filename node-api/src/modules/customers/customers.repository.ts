import { pool } from "../../config/database";

export interface Customer {
  id: number;
  tenant_id: number;
  display_name: string | null;
  phone: string | null;
  email: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export const customersRepository = {
  async create(
    tenantId: number,
    data: { displayName?: string; phone?: string; email?: string }
  ): Promise<Customer> {
    const { rows } = await pool.query<Customer>(
      `INSERT INTO customers (tenant_id, display_name, phone, email)
       VALUES ($1, $2, $3, $4) RETURNING *`,
      [tenantId, data.displayName ?? null, data.phone ?? null, data.email ?? null]
    );
    return rows[0];
  },

  async findById(tenantId: number, customerId: number): Promise<Customer | null> {
    const { rows } = await pool.query<Customer>(
      `SELECT * FROM customers WHERE tenant_id = $1 AND id = $2`,
      [tenantId, customerId]
    );
    return rows[0] ?? null;
  },

  /**
   * Finds (or implicitly signals the need to create) a customer by their
   * identity on a specific channel. This is the "unified customer
   * identity" lookup described in architecture doc section 4 — it never
   * guesses; it only matches on an explicit channel identity link.
   */
  async findByChannelIdentity(
    tenantId: number,
    channel: string,
    externalId: string
  ): Promise<Customer | null> {
    const { rows } = await pool.query<Customer>(
      `SELECT c.*
         FROM customers c
         JOIN customer_channel_identities ci
           ON ci.customer_id = c.id AND ci.tenant_id = c.tenant_id
        WHERE ci.tenant_id = $1 AND ci.channel = $2 AND ci.external_id = $3`,
      [tenantId, channel, externalId]
    );
    return rows[0] ?? null;
  },

  async linkChannelIdentity(
    tenantId: number,
    customerId: number,
    channel: string,
    externalId: string
  ): Promise<void> {
    await pool.query(
      `INSERT INTO customer_channel_identities (tenant_id, customer_id, channel, external_id)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (tenant_id, channel, external_id) DO NOTHING`,
      [tenantId, customerId, channel, externalId]
    );
  },

  /**
   * Exact match only — deterministic identity signal for Identity
   * Resolution (see identity_resolution.service.ts). Never used for
   * fuzzy/similarity matching; phone numbers must be byte-identical
   * (channel adapters are responsible for normalizing format before
   * calling this, e.g. consistent country-code prefixing).
   */
  async findByPhone(tenantId: number, phone: string): Promise<Customer | null> {
    const { rows } = await pool.query<Customer>(
      `SELECT * FROM customers WHERE tenant_id = $1 AND phone = $2`,
      [tenantId, phone]
    );
    return rows[0] ?? null;
  },

  /** Exact match only — see findByPhone's note above. Note: customers.create()
   * does not normalize email case, so this deliberately doesn't either —
   * a case-insensitive `findByEmail` would falsely imply the column is
   * stored case-normalized when it isn't. */
  async findByEmail(tenantId: number, email: string): Promise<Customer | null> {
    const { rows } = await pool.query<Customer>(
      `SELECT * FROM customers WHERE tenant_id = $1 AND email = $2`,
      [tenantId, email]
    );
    return rows[0] ?? null;
  },
};
