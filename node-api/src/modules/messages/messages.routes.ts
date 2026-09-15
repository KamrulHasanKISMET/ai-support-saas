import { Router } from "express";
import { TenantRequest } from "../../middleware/tenant";
import { RequestIdRequest } from "../../middleware/requestId";
import { AppError } from "../../middleware/errorHandler";
import { processIncomingMessage } from "./messages.service";

export const messagesRouter = Router();

/**
 * Single intake endpoint used by every channel adapter (website widget,
 * WhatsApp webhook, Facebook webhook, etc.). Mounted under requireApiKey
 * in app.ts -- this whole router is server-to-server only. Reading
 * message history back out for a dashboard is a SEPARATE, staff-authed
 * concern -- see GET /conversations/:id/messages instead.
 *
 * This route is now a thin wrapper around processIncomingMessage()
 * (messages.service.ts) -- the WhatsApp webhook (channels.routes.ts)
 * calls the exact same function, so there is only ONE implementation
 * of the Channel -> Identity -> Conversation -> Kernel -> Reply flow.
 */
messagesRouter.post("/", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const { customerId, channel, content } = req.body as {
      customerId: number;
      channel: string;
      content: string;
    };

    if (!customerId || !channel || !content) {
      throw new AppError(
        "VALIDATION_ERROR",
        "customerId, channel and content are required."
      );
    }

    const result = await processIncomingMessage({
      tenantId,
      customerId,
      channel,
      content,
      requestId: (req as unknown as RequestIdRequest).requestId,
    });

    res.status(201).json(result);
  } catch (err) {
    next(err);
  }
});
