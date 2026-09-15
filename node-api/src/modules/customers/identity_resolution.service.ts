import type { Customer } from "./customers.repository";

// =====================================================================
// IDENTITY RESOLUTION — architectural foundation only.
//
// Responsibility: given a channel-identity signal (and optionally a
// phone/email also observed on this message), decide whether this is
// an already-known customer or a new one — using ONLY deterministic,
// exact-match signals. Never merges customers based on name, writing
// style, or any kind of similarity/fuzzy matching (explicit task
// constraint) — that kind of matching produces false merges (two
// different people who happen to share a common name) and is
// unsafe for a system this simple to attempt.
//
// Where this belongs: node-api, NOT python-api — customer identity is
// persistence/business data (architecture doc rule 4: "Node.js does
// routing/persistence... Python does reasoning"). It reuses the
// EXISTING customer_channel_identities table and
// findByChannelIdentity/linkChannelIdentity repository methods (see
// PROJECT_STATUS.md — these already existed, unused by any route).
// This file adds a phone/email exact-match fallback tier and a single
// clear resolveCustomer() interface, still not called from any route.
//
// Why this isn't wired into any route yet: the only realistic caller
// would be a channel webhook handler (WhatsApp/Facebook), and those
// don't exist yet (channels.routes.ts is a stub — see
// docs/ROADMAP.md item 6). Wiring this in now would mean guessing at
// an integration point that doesn't exist rather than building a real
// one. This module is complete and independently tested; a future
// webhook handler can call `resolveCustomer(customersRepository, ...)`
// directly once built.
//
// Priority order (strictly deterministic, first match wins):
//   1. Exact channel-identity match (same channel + same external id)
//   2. Exact phone match (if a phone number was observed on this message)
//   3. Exact email match (if an email was observed on this message)
//   4. No match -> caller should create a new customer + link identity
// =====================================================================

/**
 * The subset of customersRepository this module actually needs,
 * expressed as its own interface (TypeScript structural typing means
 * the real customersRepository already satisfies this — no adapter
 * needed). Keeping this as a separate, minimal interface (rather than
 * importing the concrete repository) keeps this file free of any
 * import that would require pg/express types to compile, and makes it
 * trivial to pass a fake implementation in tests.
 */
export interface CustomerIdentityRepository {
  findByChannelIdentity(
    tenantId: number,
    channel: string,
    externalId: string
  ): Promise<Customer | null>;
  findByPhone(tenantId: number, phone: string): Promise<Customer | null>;
  findByEmail(tenantId: number, email: string): Promise<Customer | null>;
  linkChannelIdentity(
    tenantId: number,
    customerId: number,
    channel: string,
    externalId: string
  ): Promise<void>;
}

export interface IdentitySignals {
  tenantId: number;
  channel: string;
  externalId: string;
  /** Optional — only used as an exact-match fallback signal, never fuzzy-matched. */
  phone?: string;
  /** Optional — only used as an exact-match fallback signal, never fuzzy-matched. */
  email?: string;
}

export interface IdentityResolutionResult {
  customer: Customer | null;
  /** Which deterministic signal produced the match, for observability —
   * never used to decide anything downstream, just for logging/tracing. */
  matchedBy: "channel_identity" | "phone" | "email" | "none";
}

/**
 * Resolves an existing customer using ONLY deterministic signals, in
 * strict priority order. Does NOT create a customer or link an
 * identity — that decision belongs to the caller (e.g. a webhook
 * handler decides what to do with `matchedBy: "none"`), keeping this
 * function a pure lookup with no side effects.
 */
export async function resolveCustomer(
  repository: CustomerIdentityRepository,
  signals: IdentitySignals
): Promise<IdentityResolutionResult> {
  const byChannel = await repository.findByChannelIdentity(
    signals.tenantId,
    signals.channel,
    signals.externalId
  );
  if (byChannel) {
    return { customer: byChannel, matchedBy: "channel_identity" };
  }

  if (signals.phone) {
    const byPhone = await repository.findByPhone(signals.tenantId, signals.phone);
    if (byPhone) {
      return { customer: byPhone, matchedBy: "phone" };
    }
  }

  if (signals.email) {
    const byEmail = await repository.findByEmail(signals.tenantId, signals.email);
    if (byEmail) {
      return { customer: byEmail, matchedBy: "email" };
    }
  }

  return { customer: null, matchedBy: "none" };
}

/**
 * Convenience wrapper: resolves an existing customer via
 * resolveCustomer(), or links the given channel identity to
 * `fallbackCustomerId` if none was found — e.g. a webhook handler that
 * already created a new customer row and now needs the channel
 * identity linked to it. Still no fuzzy matching, still deterministic.
 */
export async function resolveOrLinkCustomer(
  repository: CustomerIdentityRepository,
  signals: IdentitySignals,
  fallbackCustomerId: number
): Promise<IdentityResolutionResult> {
  const result = await resolveCustomer(repository, signals);
  if (result.customer) {
    return result;
  }
  await repository.linkChannelIdentity(
    signals.tenantId,
    fallbackCustomerId,
    signals.channel,
    signals.externalId
  );
  return result;
}
