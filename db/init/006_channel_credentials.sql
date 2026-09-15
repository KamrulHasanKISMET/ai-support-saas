-- =====================================================================
-- CHANNEL CREDENTIALS
-- One row per tenant per channel. Lets a single shared webhook URL
-- (one Meta App, e.g.) route an incoming message to the right tenant
-- by looking up the channel-specific account id the message arrived
-- on (WhatsApp: phone_number_id), and lets Node.js send replies back
-- using that tenant's own access token.
--
-- NOTE: access_token is stored as plaintext here for MVP simplicity.
-- Before real production use, this should move to an encrypted column
-- or a secrets manager -- flagged explicitly, not glossed over.
-- =====================================================================

CREATE TABLE channel_credentials (
    id                    SERIAL PRIMARY KEY,
    tenant_id             INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel               VARCHAR(50) NOT NULL,        -- 'whatsapp' | 'facebook' (future)
    external_account_id   VARCHAR(255) NOT NULL,       -- WhatsApp: phone_number_id
    access_token          TEXT NOT NULL,               -- per-tenant token to send messages as this account
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(tenant_id, channel),           -- one connection per tenant per channel (MVP)
    UNIQUE(channel, external_account_id)  -- lets the webhook find the tenant from the payload
);
CREATE INDEX idx_channel_credentials_lookup ON channel_credentials(channel, external_account_id);
CREATE INDEX idx_channel_credentials_tenant ON channel_credentials(tenant_id);
