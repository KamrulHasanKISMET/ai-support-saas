import axios from "axios";
import { env } from "../../config/env";
import { logger } from "../../utils/logger";

// =====================================================================
// WHATSAPP OUTBOUND MESSAGE CLIENT
//
// Calls Meta's Graph API to send a text message back to a customer.
// One call per reply -- no batching/queueing in this version (see
// node-api/src/queues/README.md for where a retry queue would plug in
// if delivery reliability becomes a problem at scale).
//
// `accessToken` is per-tenant (from channel_credentials), never a
// global secret -- each tenant's WhatsApp number has its own token,
// unlike whatsappAppSecret/whatsappVerifyToken which ARE global
// (one Meta App). Mixing these up would send a reply using the wrong
// tenant's identity.
// =====================================================================

export interface SendWhatsAppTextMessageInput {
  phoneNumberId: string;
  accessToken: string;
  to: string; // customer's WhatsApp id (phone number, no leading +)
  body: string;
}

export async function sendWhatsAppTextMessage(
  input: SendWhatsAppTextMessageInput
): Promise<void> {
  const url = `${env.whatsappGraphApiBaseUrl}/${input.phoneNumberId}/messages`;

  try {
    await axios.post(
      url,
      {
        messaging_product: "whatsapp",
        to: input.to,
        type: "text",
        text: { body: input.body },
      },
      {
        headers: {
          Authorization: `Bearer ${input.accessToken}`,
          "Content-Type": "application/json",
        },
        timeout: 15_000,
      }
    );
  } catch (err) {
    // A failed outbound send must not crash the webhook handler -- the
    // customer's message was already processed and stored; losing the
    // reply delivery is bad but recoverable (support staff can see the
    // conversation in the dashboard), a 500 to Meta's webhook is worse
    // (Meta will retry the WHOLE webhook, reprocessing the inbound
    // message too, which the Redis dedupe guard is there to catch, but
    // simpler to just not throw here at all).
    logger.error("whatsapp_send_failed", {
      phoneNumberId: input.phoneNumberId,
      to: input.to,
      error: err instanceof Error ? err.message : String(err),
    });
  }
}
