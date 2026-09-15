import express from "express";
import cors from "cors";
import helmet from "helmet";
import path from "path";

import { requestId } from "./middleware/requestId";
import { metricsMiddleware } from "./middleware/metrics";
import { requireApiKey, requireAuth } from "./middleware/auth";
import { rateLimit } from "./middleware/rateLimiter";
import { errorHandler } from "./middleware/errorHandler";
import { captureRawBody } from "./middleware/rawBody";

import { authRouter } from "./modules/auth/auth.routes";
import { tenantsRouter } from "./modules/tenants/tenants.routes";
import { customersRouter } from "./modules/customers/customers.routes";
import { conversationsRouter } from "./modules/conversations/conversations.routes";
import { messagesRouter } from "./modules/messages/messages.routes";
import { channelsRouter } from "./modules/channels/channels.routes";
import { whatsappWebhookRouter } from "./modules/channels/whatsapp.webhook";
import { productsRouter } from "./modules/products/products.routes";
import { ordersRouter } from "./modules/orders/orders.routes";
import { billingRouter } from "./modules/billing/billing.routes";
import { healthRouter, readinessRouter, metricsRouter } from "./modules/health/health.routes";

export function createApp() {
  const app = express();

  app.use(helmet());
  app.use(cors());
  // `verify` stashes the raw bytes on req.rawBody -- required by the
  // WhatsApp webhook's signature check (whatsapp.signature.ts), which
  // must hash the exact bytes Meta sent, not a re-serialized copy.
  app.use(express.json({ verify: captureRawBody }));
  app.use(requestId);
  // Phase 1 observability: times every request, records it in
  // utils/metrics.ts, logs one structured line. Mounted right after
  // requestId so req.requestId is already set for the log line.
  app.use(metricsMiddleware);

  // "Is the process alive?" -- no dependency calls. Distinct from
  // /readiness below ("can this service currently serve requests?").
  app.use("/health", healthRouter);
  app.use("/readiness", readinessRouter);
  app.use("/metrics", requireAuth, metricsRouter);

  // Minimal, dependency-free static page(s) -- e.g. /connect-whatsapp.html --
  // served directly by node-api while the real dashboard (web/, see
  // PROJECT_STATUS.md) doesn't exist yet. Plain HTML/CSS/JS, no build
  // step; calls the JSON API below at the same origin.
  app.use(express.static(path.join(__dirname, "..", "public")));

  // Platform-level / auth (no tenant context required yet)
  app.use("/auth", authRouter);
  app.use("/tenants", tenantsRouter);

  // Inbound webhooks: called DIRECTLY by the channel provider (Meta),
  // not by our own channel adapters or dashboard. Cannot be behind
  // requireApiKey/requireAuth -- Meta has no way to send those headers.
  // Each webhook route verifies the provider's own signature instead
  // (see whatsapp.webhook.ts) -- that check IS this route's auth.
  app.use("/webhooks/whatsapp", whatsappWebhookRouter);

  // Channel-facing routes: server-to-server calls from OUR OWN channel
  // adapters (currently just the website widget). Authenticated with a
  // per-tenant x-api-key -- never a client-declared tenant id.
  app.use("/customers", requireApiKey, customersRouter);
  app.use(
    "/messages",
    requireApiKey,
    rateLimit(60, 60), // 60 messages/tenant/minute -- tune per plan later
    messagesRouter
  );

  // Dashboard-facing routes: staff logged in via /auth/login, JWT-derived
  // tenant/user identity -- never a client-declared tenant id.
  // /channels here is credential MANAGEMENT (connect your WhatsApp
  // number) -- distinct from /webhooks/whatsapp above, which is the
  // actual inbound message receiver.
  app.use("/channels", requireAuth, channelsRouter);
  app.use("/conversations", requireAuth, conversationsRouter);
  app.use("/products", requireAuth, productsRouter);
  app.use("/orders", requireAuth, ordersRouter);
  app.use("/billing", requireAuth, billingRouter);

  app.use(errorHandler);

  return app;
}
