import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createHmac } from "node:crypto";
import { verifyWhatsAppSignature } from "./whatsapp.signature";

function sign(body: Buffer, secret: string): string {
  return "sha256=" + createHmac("sha256", secret).update(body).digest("hex");
}

test("accepts a correctly signed body", () => {
  const secret = "test-app-secret";
  const body = Buffer.from(JSON.stringify({ hello: "world" }));
  const signature = sign(body, secret);

  assert.equal(verifyWhatsAppSignature(body, signature, secret), true);
});

test("rejects a signature computed with the wrong secret", () => {
  const body = Buffer.from(JSON.stringify({ hello: "world" }));
  const signature = sign(body, "wrong-secret");

  assert.equal(verifyWhatsAppSignature(body, signature, "test-app-secret"), false);
});

test("rejects when the body was tampered with after signing", () => {
  const secret = "test-app-secret";
  const originalBody = Buffer.from(JSON.stringify({ amount: 100 }));
  const signature = sign(originalBody, secret);
  const tamperedBody = Buffer.from(JSON.stringify({ amount: 100000 }));

  assert.equal(verifyWhatsAppSignature(tamperedBody, signature, secret), false);
});

test("rejects a missing signature header", () => {
  const body = Buffer.from("{}");
  assert.equal(verifyWhatsAppSignature(body, undefined, "test-app-secret"), false);
});

test("rejects a signature header without the sha256= prefix", () => {
  const secret = "test-app-secret";
  const body = Buffer.from("{}");
  const rawHex = createHmac("sha256", secret).update(body).digest("hex");

  assert.equal(verifyWhatsAppSignature(body, rawHex, secret), false);
});

test("rejects when the app secret is empty (fails closed, never open)", () => {
  const body = Buffer.from("{}");
  const signature = sign(body, "");

  assert.equal(verifyWhatsAppSignature(body, signature, ""), false);
});

test("rejects a malformed (non-hex, wrong-length) signature without throwing", () => {
  const body = Buffer.from("{}");
  assert.doesNotThrow(() => {
    assert.equal(
      verifyWhatsAppSignature(body, "sha256=not-valid-hex!!", "test-app-secret"),
      false
    );
  });
});
