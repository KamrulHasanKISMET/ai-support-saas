// =====================================================================
// WHATSAPP WEBHOOK PAYLOAD PARSER
//
// Pure function: JSON in, normalized messages out. No I/O, no DB, no
// network -- easy to unit test against fixture payloads matching
// WhatsApp Cloud API's real webhook shape.
//
// WhatsApp's webhook payload nests messages under
// entry[].changes[].value.messages[], alongside a SEPARATE
// entry[].changes[].value.statuses[] array used for delivery/read
// receipts -- those are NOT customer messages and must be ignored,
// not treated as incoming text.
//
// Only `type: "text"` messages are handled in this version --
// image/audio/video/interactive/button messages are recognized but
// SKIPPED (counted, not silently dropped without a trace), matching
// this project's existing "no multimodal yet" boundary.
// =====================================================================

export interface WhatsAppTextMessage {
  phoneNumberId: string; // which of OUR tenant's numbers received this
  from: string; // customer's WhatsApp id (phone number, no +)
  waMessageId: string; // WhatsApp's own message id -- used for de-duplication
  text: string;
  timestamp: string;
  contactName: string | null;
}

export interface ParsedWhatsAppWebhook {
  textMessages: WhatsAppTextMessage[];
  skippedNonTextCount: number;
}

/** Loosely typed on purpose -- this is untrusted third-party JSON;
 * every field access below is defensive (optional chaining + fallbacks). */
export function parseWhatsAppWebhookPayload(body: unknown): ParsedWhatsAppWebhook {
  const textMessages: WhatsAppTextMessage[] = [];
  let skippedNonTextCount = 0;

  const entries = (body as { entry?: unknown[] })?.entry;
  if (!Array.isArray(entries)) {
    return { textMessages, skippedNonTextCount };
  }

  for (const entry of entries) {
    const changes = (entry as { changes?: unknown[] })?.changes;
    if (!Array.isArray(changes)) continue;

    for (const change of changes) {
      const value = (change as { value?: Record<string, unknown> })?.value;
      if (!value) continue;

      const phoneNumberId = (value.metadata as { phone_number_id?: string })
        ?.phone_number_id;
      if (!phoneNumberId) continue;

      const contacts = value.contacts as
        | { profile?: { name?: string }; wa_id?: string }[]
        | undefined;
      const contactNameByWaId = new Map<string, string>();
      for (const contact of contacts ?? []) {
        if (contact.wa_id && contact.profile?.name) {
          contactNameByWaId.set(contact.wa_id, contact.profile.name);
        }
      }

      // `statuses` (delivery/read receipts) are NOT customer messages --
      // intentionally not iterated here at all.
      const messages = value.messages as
        | {
            from?: string;
            id?: string;
            timestamp?: string;
            type?: string;
            text?: { body?: string };
          }[]
        | undefined;

      for (const message of messages ?? []) {
        if (!message.from || !message.id) {
          skippedNonTextCount += 1;
          continue;
        }
        if (message.type !== "text" || !message.text?.body) {
          skippedNonTextCount += 1;
          continue;
        }

        textMessages.push({
          phoneNumberId,
          from: message.from,
          waMessageId: message.id,
          text: message.text.body,
          timestamp: message.timestamp ?? "",
          contactName: contactNameByWaId.get(message.from) ?? null,
        });
      }
    }
  }

  return { textMessages, skippedNonTextCount };
}
