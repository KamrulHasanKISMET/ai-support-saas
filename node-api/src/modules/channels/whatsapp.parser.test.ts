import { strict as assert } from "node:assert";
import { test } from "node:test";
import { parseWhatsAppWebhookPayload } from "./whatsapp.parser";

function makeTextMessagePayload(overrides: {
  phoneNumberId?: string;
  from?: string;
  messageId?: string;
  text?: string;
  contactName?: string;
} = {}) {
  return {
    entry: [
      {
        changes: [
          {
            value: {
              metadata: { phone_number_id: overrides.phoneNumberId ?? "1000000" },
              contacts: [
                {
                  profile: { name: overrides.contactName ?? "Kamrul" },
                  wa_id: overrides.from ?? "8801700000000",
                },
              ],
              messages: [
                {
                  from: overrides.from ?? "8801700000000",
                  id: overrides.messageId ?? "wamid.ABC123",
                  timestamp: "1700000000",
                  type: "text",
                  text: { body: overrides.text ?? "amar order ta kobe pabo?" },
                },
              ],
            },
          },
        ],
      },
    ],
  };
}

test("parses a normal incoming text message", () => {
  const payload = makeTextMessagePayload();
  const result = parseWhatsAppWebhookPayload(payload);

  assert.equal(result.textMessages.length, 1);
  assert.equal(result.skippedNonTextCount, 0);
  const msg = result.textMessages[0];
  assert.equal(msg.phoneNumberId, "1000000");
  assert.equal(msg.from, "8801700000000");
  assert.equal(msg.waMessageId, "wamid.ABC123");
  assert.equal(msg.text, "amar order ta kobe pabo?");
  assert.equal(msg.contactName, "Kamrul");
});

test("ignores status updates (delivered/read receipts), not treated as messages", () => {
  const payload = {
    entry: [
      {
        changes: [
          {
            value: {
              metadata: { phone_number_id: "1000000" },
              statuses: [
                { id: "wamid.XYZ", status: "delivered", timestamp: "1700000000" },
              ],
              // no `messages` array at all in a pure status-update payload
            },
          },
        ],
      },
    ],
  };

  const result = parseWhatsAppWebhookPayload(payload);
  assert.equal(result.textMessages.length, 0);
  assert.equal(result.skippedNonTextCount, 0);
});

test("skips (but counts) non-text message types like image/audio", () => {
  const payload = {
    entry: [
      {
        changes: [
          {
            value: {
              metadata: { phone_number_id: "1000000" },
              messages: [
                {
                  from: "8801700000000",
                  id: "wamid.IMG1",
                  timestamp: "1700000000",
                  type: "image",
                  image: { id: "media-id-123" },
                },
              ],
            },
          },
        ],
      },
    ],
  };

  const result = parseWhatsAppWebhookPayload(payload);
  assert.equal(result.textMessages.length, 0);
  assert.equal(result.skippedNonTextCount, 1);
});

test("handles multiple messages across multiple entries/changes", () => {
  const payload = {
    entry: [
      {
        changes: [
          { value: makeTextMessagePayload({ messageId: "wamid.1", text: "hi" }).entry[0].changes[0].value },
        ],
      },
      {
        changes: [
          { value: makeTextMessagePayload({ messageId: "wamid.2", text: "bye" }).entry[0].changes[0].value },
        ],
      },
    ],
  };

  const result = parseWhatsAppWebhookPayload(payload);
  assert.equal(result.textMessages.length, 2);
  assert.deepEqual(
    result.textMessages.map((m) => m.waMessageId),
    ["wamid.1", "wamid.2"]
  );
});

test("returns empty result for malformed/unexpected payload shapes without throwing", () => {
  assert.deepEqual(parseWhatsAppWebhookPayload({}), {
    textMessages: [],
    skippedNonTextCount: 0,
  });
  assert.deepEqual(parseWhatsAppWebhookPayload(null), {
    textMessages: [],
    skippedNonTextCount: 0,
  });
  assert.deepEqual(parseWhatsAppWebhookPayload("not even an object"), {
    textMessages: [],
    skippedNonTextCount: 0,
  });
  assert.deepEqual(parseWhatsAppWebhookPayload({ entry: "not-an-array" }), {
    textMessages: [],
    skippedNonTextCount: 0,
  });
});

test("skips a message missing required fields (from/id) rather than crashing", () => {
  const payload = {
    entry: [
      {
        changes: [
          {
            value: {
              metadata: { phone_number_id: "1000000" },
              messages: [{ type: "text", text: { body: "incomplete" } }], // no `from`, no `id`
            },
          },
        ],
      },
    ],
  };

  const result = parseWhatsAppWebhookPayload(payload);
  assert.equal(result.textMessages.length, 0);
  assert.equal(result.skippedNonTextCount, 1);
});

test("contactName is null when no matching contact profile exists", () => {
  const payload = {
    entry: [
      {
        changes: [
          {
            value: {
              metadata: { phone_number_id: "1000000" },
              // no `contacts` array at all
              messages: [
                {
                  from: "8801700000000",
                  id: "wamid.NOPROFILE",
                  timestamp: "1700000000",
                  type: "text",
                  text: { body: "hello" },
                },
              ],
            },
          },
        ],
      },
    ],
  };

  const result = parseWhatsAppWebhookPayload(payload);
  assert.equal(result.textMessages.length, 1);
  assert.equal(result.textMessages[0].contactName, null);
});
