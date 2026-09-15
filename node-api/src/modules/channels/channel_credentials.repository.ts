import { pool } from "../../config/database";

export interface ChannelCredential {
  id: number;
  tenant_id: number;
  channel: string;
  external_account_id: string;
  access_token: string;
  is_active: boolean;
}

export const channelCredentialsRepository = {
  /**
   * The lookup a webhook uses: given the channel-specific account id
   * the message ARRIVED on (WhatsApp's phone_number_id), find which
   * tenant owns it. Exact match only -- same deterministic-signal
   * discipline as identity_resolution.service.ts.
   */
  async findByChannelAndAccountId(
    channel: string,
    externalAccountId: string
  ): Promise<ChannelCredential | null> {
    const { rows } = await pool.query<ChannelCredential>(
      `SELECT * FROM channel_credentials
        WHERE channel = $1 AND external_account_id = $2 AND is_active = TRUE`,
      [channel, externalAccountId]
    );
    return rows[0] ?? null;
  },

  async findByTenantAndChannel(
    tenantId: number,
    channel: string
  ): Promise<ChannelCredential | null> {
    const { rows } = await pool.query<ChannelCredential>(
      `SELECT * FROM channel_credentials WHERE tenant_id = $1 AND channel = $2`,
      [tenantId, channel]
    );
    return rows[0] ?? null;
  },

  /** Connect (or reconnect/rotate the token for) a tenant's channel account. */
  async upsert(
    tenantId: number,
    channel: string,
    externalAccountId: string,
    accessToken: string
  ): Promise<ChannelCredential> {
    const { rows } = await pool.query<ChannelCredential>(
      `INSERT INTO channel_credentials (tenant_id, channel, external_account_id, access_token)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (tenant_id, channel)
       DO UPDATE SET external_account_id = EXCLUDED.external_account_id,
                     access_token = EXCLUDED.access_token,
                     is_active = TRUE,
                     updated_at = CURRENT_TIMESTAMP
       RETURNING *`,
      [tenantId, channel, externalAccountId, accessToken]
    );
    return rows[0];
  },
};
