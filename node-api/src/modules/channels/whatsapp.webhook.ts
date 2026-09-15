import { Router } from "express";
import { env } from "../../config/env";
import { redis } from "../../config/redis";
import { logger } from "../../utils/logger";
import { RawBodyRequest } from "../../middleware/rawBody";
import { verifyWhatsAppSignature } from "./whatsapp.signature";
import { parseWhatsAppWebhookPayload, WhatsAppTextMessage } from "./whatsapp.parser";
import { channelCredentialsRepository } from "./channel_credentials.repository";
import { sendWhatsAppTextMessage } from "./whatsapp.client";
import { resolveCustomer } from "../customers/identity_resolution.service";
import { customersRepository } from "../customers/customers.repository";
import { processIncomingMessage } from "../messages/messages.service";

// =====================================================================
// WHATSAPP WEBHOOK — the real integration point Meta calls directly.
//
// NOT behind requireApiKey/requireAuth (see app.ts) — Meta cannot send
// our own auth headers. Signature verification (whatsapp.signature.ts)
// IS this endpoint's authentication, checked on every POST below.
//
// Flow for each inbound text message (mirrors architecture doc section
// 4, using pieces that already existed rather than duplicating them):
//   1. De-duplicate (Meta retries webhooks; wamid makes retries safe)
//   2. Look up which tenant owns this phone_number_id (channel_credentials)
//   3. Resolve or create the customer -- deterministic only
//      (identity_resolution.service.ts, never fuzzy/name-based)
//   4. processIncomingMessage() -- the SAME function POST /messages
//      uses, so there is only one Channel -> Kernel -> Reply pipeline
//   5. Send the AI's reply back via the WhatsApp Cloud API
// =====================================================================

export const whatsappWebhookRouter = Router();

/** Meta's one-time webhook verification handshake, done when you
 * configure the webhook URL in the Meta App dashboard. */
whatsappWebhookRouter.get("/", (req, res) => {
  const mode = req.query["hub.mode"];
  const token = req.query["hub.verify_token"];
  const challenge = req.query["hub.challenge"];

  if (
    env.whatsappVerifyToken &&
    mode === "subscribe" &&
    token === env.whatsappVerifyToken
  ) {
    res.status(200).send(challenge);
    return;
  }
  res.sendStatus(403);
});

whatsappWebhookRouter.post("/", async (req: RawBodyRequest, res) => {
  const signatureValid = verifyWhatsAppSignature(
    req.rawBody ?? Buffer.alloc(0),
    req.header("x-hub-signature-256"),
    env.whatsappAppSecret
  );

  if (!signatureValid) {
    logger.error("whatsapp_invalid_signature", {});
    res.sendStatus(401);
    return;
  }

  // Ack Meta immediately — it expects a fast 200 and will retry
  // aggressively (and duplicate-send) if the webhook is slow/non-200.
  // Everything below runs after responding; any failure is logged, not
  // surfaced as an HTTP error (there is no one left listening for it).
  res.sendStatus(200);

  let parsed;
  try {
    parsed = parseWhatsAppWebhookPayload(req.body);
  } catch (err) {
    logger.error("whatsapp_payload_parse_failed", {
      error: err instanceof Error ? err.message : String(err),
    });
    return;
  }

  for (const message of parsed.textMessages) {
    try {
      await handleIncomingWhatsAppMessage(message);
    } catch (err) {
      logger.error("whatsapp_message_processing_failed", {
        waMessageId: message.waMessageId,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }
});

async function handleIncomingWhatsAppMessage(
  message: WhatsAppTextMessage
): Promise<void> {
  // 1. De-duplicate. redis.set(..., "NX") returns null if the key
  // already existed -- i.e. we've already processed this exact
  // WhatsApp message id (a Meta retry), so skip it.
  const dedupeKey = `whatsapp:dedupe:${message.waMessageId}`;
  const firstSeen = await redis.set(dedupeKey, "1", "EX", 86_400, "NX");
  if (firstSeen === null) {
    logger.info("whatsapp_duplicate_message_skipped", {
      waMessageId: message.waMessageId,
    });
    return;
  }

  // 2. Which tenant owns the phone number this arrived on?
  const credential = await channelCredentialsRepository.findByChannelAndAccountId(
    "whatsapp",
    message.phoneNumberId
  );
  if (!credential) {
    logger.error("whatsapp_unknown_phone_number_id", {
      phoneNumberId: message.phoneNumberId,
    });
    return;
  }
  const tenantId = credential.tenant_id;

  // 3. Resolve or create the customer — deterministic signals only
  // (channel identity, then exact phone match). Never guesses by name.
  const resolution = await resolveCustomer(customersRepository, {
    tenantId,
    channel: "whatsapp",
    externalId: message.from,
    phone: message.from,
  });

  let customerId: number;
  if (resolution.customer) {
    customerId = resolution.customer.id;
  } else {
    const newCustomer = await customersRepository.create(tenantId, {
      displayName: message.contactName ?? undefined,
      phone: message.from,
    });
    await customersRepository.linkChannelIdentity(
      tenantId,
      newCustomer.id,
      "whatsapp",
      message.from
    );
    customerId = newCustomer.id;
  }

  // 4. The SAME Channel -> Kernel -> Reply flow POST /messages uses —
  // no second, drifted implementation of this pipeline.
  const result = await processIncomingMessage({
    tenantId,
    customerId,
    channel: "whatsapp",
    content: message.text,
  });

  // 5. Reply, using THIS tenant's own access token — never a shared
  // or wrong-tenant token.
  await sendWhatsAppTextMessage({
    phoneNumberId: message.phoneNumberId,
    accessToken: credential.access_token,
    to: message.from,
    body: result.aiMessage.content,
  });
}
