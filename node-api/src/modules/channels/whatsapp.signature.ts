import { createHmac, timingSafeEqual } from "crypto";

// =====================================================================
// WHATSAPP WEBHOOK SIGNATURE VERIFICATION
//
// Meta signs every webhook POST body with HMAC-SHA256 using the Meta
// App's app secret, sent as `X-Hub-Signature-256: sha256=<hex>`. This
// is what actually authenticates an incoming webhook call -- NOT our
// own x-api-key scheme, which is for OUR channel adapters calling OUR
// API, not applicable to Meta calling us. A request with a
// missing/invalid signature must be rejected before anything in it is
// trusted (payload contents, tenant lookup, etc.).
//
// One Meta App = one app secret, shared across every tenant's phone
// number registered under that app -- this is a global secret
// (WHATSAPP_APP_SECRET), not per-tenant.
// =====================================================================

/**
 * Verifies `X-Hub-Signature-256` against the RAW request body bytes.
 * MUST be called with the raw bytes exactly as received (before any
 * JSON parsing/re-serialization) -- re-serializing and re-hashing a
 * parsed-then-stringified body will not match Meta's signature due to
 * key ordering/whitespace differences. See app.ts's `verify` callback
 * on express.json(), which stashes the raw buffer for this purpose.
 */
export function verifyWhatsAppSignature(
  rawBody: Buffer,
  signatureHeader: string | undefined,
  appSecret: string
): boolean {
  if (!signatureHeader || !appSecret) {
    return false;
  }

  const expectedPrefix = "sha256=";
  if (!signatureHeader.startsWith(expectedPrefix)) {
    return false;
  }
  const providedHex = signatureHeader.slice(expectedPrefix.length);

  const computedHex = createHmac("sha256", appSecret).update(rawBody).digest("hex");

  // Constant-time comparison -- a naive `===` on secret-derived values
  // leaks timing information an attacker could use to forge a valid
  // signature byte-by-byte.
  const providedBuffer = Buffer.from(providedHex, "hex");
  const computedBuffer = Buffer.from(computedHex, "hex");
  if (providedBuffer.length !== computedBuffer.length) {
    return false;
  }
  return timingSafeEqual(providedBuffer, computedBuffer);
}
