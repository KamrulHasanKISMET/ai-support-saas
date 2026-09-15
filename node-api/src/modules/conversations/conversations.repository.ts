import { pool } from "../../config/database";

export interface Conversation {
  id: number;
  tenant_id: number;
  customer_id: number;
  channel: string;
  status: string;
  created_at: string;
}

export const conversationsRepository = {
  async create(
    tenantId: number,
    customerId: number,
    channel: string
  ): Promise<Conversation> {
    const { rows } = await pool.query<Conversation>(
      `INSERT INTO conversations (tenant_id, customer_id, channel)
       VALUES ($1, $2, $3) RETURNING *`,
      [tenantId, customerId, channel]
    );
    return rows[0];
  },

  /** Returns the most recent open conversation for a customer, if any. */
  async findOpenForCustomer(
    tenantId: number,
    customerId: number
  ): Promise<Conversation | null> {
    const { rows } = await pool.query<Conversation>(
      `SELECT * FROM conversations
        WHERE tenant_id = $1 AND customer_id = $2 AND status = 'open'
        ORDER BY created_at DESC
        LIMIT 1`,
      [tenantId, customerId]
    );
    return rows[0] ?? null;
  },

  async findById(tenantId: number, id: number): Promise<Conversation | null> {
    const { rows } = await pool.query<Conversation>(
      `SELECT * FROM conversations WHERE tenant_id = $1 AND id = $2`,
      [tenantId, id]
    );
    return rows[0] ?? null;
  },
};
