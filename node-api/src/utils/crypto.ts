import { randomBytes } from "crypto";

/** 48-hex-char (24-byte) random API key, e.g. for a new tenant. */
export function generateApiKey(): string {
  return randomBytes(24).toString("hex");
}
