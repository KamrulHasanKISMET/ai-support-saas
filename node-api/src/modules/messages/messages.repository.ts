import { pool } from "../../config/database";

export type SenderType = "customer" | "agent" | "ai" | "system";

export interface Message {
  id: number;
  tenant_id: number;
  conversation_id: number;
  sender_type: SenderType;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export const messagesRepository = {
  async create(
    tenantId: number,
    conversationId: number,
    senderType: SenderType,
    content: string,
    metadata: Record<string, unknown> = {}
  ): Promise<Message> {
    const { rows } = await pool.query<Message>(
      `INSERT INTO messages (tenant_id, conversation_id, sender_type, content, metadata)
       VALUES ($1, $2, $3, $4, $5) RETURNING *`,
      [tenantId, conversationId, senderType, content, JSON.stringify(metadata)]
    );
    return rows[0];
  },

  async listForConversation(
    tenantId: number,
    conversationId: number,
    limit = 50
  ): Promise<Message[]> {
    const { rows } = await pool.query<Message>(
      `SELECT * FROM messages
        WHERE tenant_id = $1 AND conversation_id = $2
        ORDER BY created_at ASC
        LIMIT $3`,
      [tenantId, conversationId, limit]
    );
    return rows;
  },
};
