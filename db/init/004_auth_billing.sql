-- =====================================================================
-- AUTH: per-tenant API key for server-to-server calls (channel adapters,
-- webhooks). This replaces blindly trusting a client-supplied
-- x-tenant-id header — every channel-facing request must now present
-- a real secret that maps to exactly one tenant.
-- =====================================================================

ALTER TABLE tenants ADD COLUMN api_key VARCHAR(64) UNIQUE;

-- Backfill any existing rows with a random key so the column can be
-- made NOT NULL going forward (new rows always generate one — see
-- node-api/src/modules/auth/auth.routes.ts).
UPDATE tenants SET api_key = encode(gen_random_bytes(24), 'hex') WHERE api_key IS NULL;

CREATE INDEX idx_tenants_api_key ON tenants(api_key);

-- =====================================================================
-- BILLING (minimal — section 6 of the architecture doc lists
-- Subscriptions/Billing as a Node.js responsibility). This only
-- records intent/state locally; it does NOT integrate a real payment
-- gateway (no keys/network available to do that honestly here). See
-- node-api/src/modules/billing/billing.routes.ts for the extension
-- point where a real provider (Stripe/SSLCommerz/bKash) would plug in.
-- =====================================================================

CREATE TABLE subscriptions (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan                VARCHAR(50) NOT NULL DEFAULT 'trial',
    status              VARCHAR(50) NOT NULL DEFAULT 'active', -- active | past_due | canceled
    current_period_end  TIMESTAMP,
    provider            VARCHAR(50),         -- e.g. 'stripe', 'manual' — null until wired up
    provider_ref        VARCHAR(255),        -- external subscription/customer id
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tenant_id)
);
