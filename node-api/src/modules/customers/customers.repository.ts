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

  /**
   * Dashboard list/search (CUSTOMER-KNOWLEDGE-RAG-API-001 phase 1).
   * `search` matches name/phone/email with a single ILIKE against all
   * three (a customer service rep rarely knows which field they're
   * typing into) -- fine at this project's current scale; if the
   * customers table gets large enough for this to show up in
   * `db_query_duration_seconds` (see docs/RELIABILITY.md), that's the
   * point to add trigram indexes rather than optimize preemptively now.
   */
  async list(
    tenantId: number,
    options: { search?: string; limit: number; offset: number }
  ): Promise<{ customers: Customer[]; total: number }> {
    const searchClause = options.search ? `AND (
        display_name ILIKE $2 OR phone ILIKE $2 OR email ILIKE $2
      )` : "";
    const params: unknown[] = options.search
      ? [tenantId, `%${options.search}%`]
      : [tenantId];

    const countResult = await pool.query<{ count: string }>(
      `SELECT COUNT(*) FROM customers WHERE tenant_id = $1 ${searchClause}`,
      params
    );

    const { rows } = await pool.query<Customer>(
      `SELECT * FROM customers WHERE tenant_id = $1 ${searchClause}
       ORDER BY created_at DESC
       LIMIT ${params.length + 1} OFFSET ${params.length + 2}`,
      [...params, options.limit, options.offset]
    );

    return { customers: rows, total: Number(countResult.rows[0].count) };
  },

  /** Returns null if the customer doesn't exist (or belongs to another
   * tenant) — the route turns that into a 404, never a 500. Partial
   * update: only columns actually present in `data` are touched, via
   * COALESCE against the existing row rather than a full column list,
   * so a PATCH with just `{ email }` can't accidentally null out
   * `display_name`/`phone`. */
  async update(
    tenantId: number,
    customerId: number,
    data: { displayName?: string; phone?: string; email?: string; metadata?: Record<string, unknown> }
  ): Promise<Customer | null> {
    const { rows } = await pool.query<Customer>(
      `UPDATE customers
          SET display_name = COALESCE($3, display_name),
              phone        = COALESCE($4, phone),
              email        = COALESCE($5, email),
              metadata     = COALESCE($6, metadata),
              updated_at   = now()
        WHERE tenant_id = $1 AND id = $2
        RETURNING *`,
      [
        tenantId,
        customerId,
        data.displayName ?? null,
        data.phone ?? null,
        data.email ?? null,
        data.metadata ? JSON.stringify(data.metadata) : null,
      ]
    );
    return rows[0] ?? null;
  },

  /** Returns true if a row was actually deleted (tenant-scoped). Relies
   * on existing FK ON DELETE behavior for dependent rows (conversations/
   * orders/customer_channel_identities) -- unchanged by this task,
   * see db/init/002_core_tables.sql. */
  async delete(tenantId: number, customerId: number): Promise<boolean> {
    const { rowCount } = await pool.query(
      `DELETE FROM customers WHERE tenant_id = $1 AND id = $2`,
      [tenantId, customerId]
    );
    return (rowCount ?? 0) > 0;
  },
};
