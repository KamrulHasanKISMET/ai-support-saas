import axios from "axios";
import { env } from "../../config/env";
import { logger } from "../../utils/logger";

export interface KernelRequestPayload {
  tenantId: number;
  customerId: number;
  conversationId: number;
  messageId: number;
  message: string;
  requestId?: string;
}

export interface KernelResponsePayload {
  reply: string;
  intent?: string;
  confidence?: number;
  state?: Record<string, unknown>;
  toolsCalled?: string[];
  // Populated by CoreAgent — currently carries { language: { detected, reply } }
  // plus whatever the caller passed in as metadata. Optional, additive.
  metadata?: Record<string, unknown>;
}

const pythonApi = axios.create({
  baseURL: env.pythonApiUrl,
  timeout: 30_000,
});

// Shown to the customer if the Python AI Service is unreachable or
// errors out. Deliberately bilingual and generic — never leaks
// internal exception details (architecture doc section 20/34).
const FALLBACK_REPLY =
  "দুঃখিত, এই মুহূর্তে সাড়া দিতে একটু সমস্যা হচ্ছে। একটু পরে আবার চেষ্টা করুন, অথবা আমাদের টিম শীঘ্রই আপনার সাথে যোগাযোগ করবে।";

/**
 * Node.js never does heavy AI processing itself (architecture doc
 * section 6/7). It hands the message to the Python AI Service, which
 * runs the Kernel (Intent -> State -> Context -> RAG -> Memory ->
 * Reasoning -> Decision -> Tools -> Response) and returns a reply.
 *
 * Controlled failure (section 20): if the AI service is down, slow, or
 * errors, this NEVER throws up to the customer as a raw 500. It logs
 * the real error for ops and returns a safe fallback reply instead —
 * the conversation flow continues, just without an AI-generated answer
 * for this turn.
 */
export async function runKernel(
  payload: KernelRequestPayload
): Promise<KernelResponsePayload> {
  const startedAt = Date.now();
  try {
    const { data } = await pythonApi.post<KernelResponsePayload>(
      "/ai/kernel/run",
      payload,
      {
        headers: {
          "x-internal-secret": env.internalServiceSecret,
          ...(payload.requestId ? { "x-request-id": payload.requestId } : {}),
        },
      }
    );
    logger.info("kernel_call_succeeded", {
      requestId: payload.requestId,
      tenantId: payload.tenantId,
      conversationId: payload.conversationId,
      latencyMs: Date.now() - startedAt,
      intent: data.intent,
    });
    return data;
  } catch (err) {
    logger.error("kernel_call_failed", {
      requestId: payload.requestId,
      tenantId: payload.tenantId,
      conversationId: payload.conversationId,
      latencyMs: Date.now() - startedAt,
      error: err instanceof Error ? err.message : String(err),
    });
    return {
      reply: FALLBACK_REPLY,
      intent: undefined,
      confidence: 0,
      state: {},
      toolsCalled: [],
      metadata: { error: true },
    };
  }
}
