import { conversationsRepository } from "../conversations/conversations.repository";
import { messagesRepository, Message } from "./messages.repository";
import { runKernel } from "../ai/ai.client";

export interface ProcessIncomingMessageInput {
  tenantId: number;
  customerId: number;
  channel: string;
  content: string;
  requestId?: string;
}

export interface ProcessIncomingMessageResult {
  conversationId: number;
  customerMessage: Message;
  aiMessage: Message;
  intent?: string;
  confidence?: number;
  language?: unknown;
}

/**
 * The ONE place that implements "Channel -> Customer Identity ->
 * Conversation -> (store message) -> AI Agent Kernel -> reply ->
 * (store AI message)" (architecture doc section 4/6/7).
 *
 * Extracted out of messages.routes.ts so the WhatsApp webhook
 * (channels.routes.ts) can call the exact same flow instead of a
 * second, drifted copy of it. The HTTP route POST /messages is now a
 * thin wrapper around this function — see messages.routes.ts.
 *
 * Node.js owns identity/conversation/persistence; Python owns the
 * actual reasoning (Kernel). Node.js never runs AI logic itself.
 */
export async function processIncomingMessage(
  input: ProcessIncomingMessageInput
): Promise<ProcessIncomingMessageResult> {
  const { tenantId, customerId, channel, content, requestId } = input;

  let conversation = await conversationsRepository.findOpenForCustomer(
    tenantId,
    customerId
  );
  if (!conversation) {
    conversation = await conversationsRepository.create(tenantId, customerId, channel);
  }

  const customerMessage = await messagesRepository.create(
    tenantId,
    conversation.id,
    "customer",
    content
  );

  const kernelResult = await runKernel({
    tenantId,
    customerId,
    conversationId: conversation.id,
    messageId: customerMessage.id,
    message: content,
    requestId,
  });

  const aiMessage = await messagesRepository.create(
    tenantId,
    conversation.id,
    "ai",
    kernelResult.reply,
    {
      intent: kernelResult.intent,
      confidence: kernelResult.confidence,
      toolsCalled: kernelResult.toolsCalled ?? [],
      ...(kernelResult.metadata ?? {}),
    }
  );

  return {
    conversationId: conversation.id,
    customerMessage,
    aiMessage,
    intent: kernelResult.intent,
    confidence: kernelResult.confidence,
    language: kernelResult.metadata?.language,
  };
}
