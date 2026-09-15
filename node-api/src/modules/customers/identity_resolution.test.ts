import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  resolveCustomer,
  resolveOrLinkCustomer,
  type CustomerIdentityRepository,
  type IdentitySignals,
} from "./identity_resolution.service";
import type { Customer } from "./customers.repository";

function makeCustomer(overrides: Partial<Customer> = {}): Customer {
  return {
    id: 1,
    tenant_id: 1,
    display_name: "Test Customer",
    phone: null,
    email: null,
    metadata: {},
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

/** In-memory fake — exact-match only, mirrors the real repository's contract. */
function makeFakeRepository(seed: {
  channelIdentities?: Record<string, Customer>; // key: `${tenantId}:${channel}:${externalId}`
  phones?: Record<string, Customer>; // key: `${tenantId}:${phone}`
  emails?: Record<string, Customer>; // key: `${tenantId}:${email}`
}): CustomerIdentityRepository & { linkedIdentities: string[] } {
  const linkedIdentities: string[] = [];
  return {
    linkedIdentities,
    async findByChannelIdentity(tenantId, channel, externalId) {
      const key = `${tenantId}:${channel}:${externalId}`;
      return seed.channelIdentities?.[key] ?? null;
    },
    async findByPhone(tenantId, phone) {
      const key = `${tenantId}:${phone}`;
      return seed.phones?.[key] ?? null;
    },
    async findByEmail(tenantId, email) {
      const key = `${tenantId}:${email}`;
      return seed.emails?.[key] ?? null;
    },
    async linkChannelIdentity(tenantId, customerId, channel, externalId) {
      linkedIdentities.push(`${tenantId}:${customerId}:${channel}:${externalId}`);
    },
  };
}

test("resolves by exact channel identity match first, even when phone/email would also match", async () => {
  const channelCustomer = makeCustomer({ id: 1 });
  const phoneCustomer = makeCustomer({ id: 2 }); // a DIFFERENT customer than the channel match
  const repo = makeFakeRepository({
    channelIdentities: { "1:whatsapp:8801700000000": channelCustomer },
    phones: { "1:01700000000": phoneCustomer },
  });

  const signals: IdentitySignals = {
    tenantId: 1,
    channel: "whatsapp",
    externalId: "8801700000000",
    phone: "01700000000",
  };

  const result = await resolveCustomer(repo, signals);
  assert.equal(result.matchedBy, "channel_identity");
  assert.equal(result.customer?.id, 1, "channel identity takes priority over phone");
});

test("falls back to exact phone match when no channel identity exists", async () => {
  const phoneCustomer = makeCustomer({ id: 5 });
  const repo = makeFakeRepository({
    phones: { "1:01700000000": phoneCustomer },
  });

  const result = await resolveCustomer(repo, {
    tenantId: 1,
    channel: "whatsapp",
    externalId: "unknown-external-id",
    phone: "01700000000",
  });

  assert.equal(result.matchedBy, "phone");
  assert.equal(result.customer?.id, 5);
});

test("falls back to exact email match only after channel and phone both miss", async () => {
  const emailCustomer = makeCustomer({ id: 9 });
  const repo = makeFakeRepository({
    emails: { "1:someone@example.com": emailCustomer },
  });

  const result = await resolveCustomer(repo, {
    tenantId: 1,
    channel: "website",
    externalId: "session-abc",
    phone: "01700000000", // present but won't match anything in this repo
    email: "someone@example.com",
  });

  assert.equal(result.matchedBy, "email");
  assert.equal(result.customer?.id, 9);
});

test("returns matchedBy 'none' and null customer when nothing matches — never guesses", async () => {
  const repo = makeFakeRepository({});

  const result = await resolveCustomer(repo, {
    tenantId: 1,
    channel: "whatsapp",
    externalId: "brand-new-number",
    phone: "01700000000",
    email: "new@example.com",
  });

  assert.equal(result.matchedBy, "none");
  assert.equal(result.customer, null);
});

test("phone/email are never used if not provided — no accidental cross-matching", async () => {
  // Repository DOES have phone/email matches available, but the caller
  // didn't observe a phone or email on this particular message — must
  // not somehow still match on them.
  const repo = makeFakeRepository({
    phones: { "1:01700000000": makeCustomer({ id: 2 }) },
    emails: { "1:someone@example.com": makeCustomer({ id: 3 }) },
  });

  const result = await resolveCustomer(repo, {
    tenantId: 1,
    channel: "whatsapp",
    externalId: "no-match-here",
    // no phone, no email provided
  });

  assert.equal(result.matchedBy, "none");
});

test("resolveOrLinkCustomer links the channel identity when no match was found", async () => {
  const repo = makeFakeRepository({});

  const result = await resolveOrLinkCustomer(
    repo,
    { tenantId: 1, channel: "whatsapp", externalId: "brand-new-number" },
    /* fallbackCustomerId */ 42
  );

  assert.equal(result.matchedBy, "none");
  assert.deepEqual(repo.linkedIdentities, ["1:42:whatsapp:brand-new-number"]);
});

test("resolveOrLinkCustomer does NOT link anything when a match was already found", async () => {
  const existing = makeCustomer({ id: 7 });
  const repo = makeFakeRepository({
    channelIdentities: { "1:whatsapp:8801700000000": existing },
  });

  const result = await resolveOrLinkCustomer(
    repo,
    { tenantId: 1, channel: "whatsapp", externalId: "8801700000000" },
    /* fallbackCustomerId */ 999
  );

  assert.equal(result.customer?.id, 7);
  assert.deepEqual(repo.linkedIdentities, [], "should not re-link an already-resolved customer");
});

test("tenant isolation: identical externalId under a different tenantId never matches", async () => {
  const tenantOneCustomer = makeCustomer({ id: 1, tenant_id: 1 });
  const repo = makeFakeRepository({
    channelIdentities: { "1:whatsapp:8801700000000": tenantOneCustomer },
  });

  const result = await resolveCustomer(repo, {
    tenantId: 2, // different tenant, same externalId
    channel: "whatsapp",
    externalId: "8801700000000",
  });

  assert.equal(result.matchedBy, "none", "must not leak tenant 1's customer into tenant 2's lookup");
});
