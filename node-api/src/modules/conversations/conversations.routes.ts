import { Router } from "express";
import { TenantRequest } from "../../middleware/tenant";
import { conversationsRepository } from "./conversations.repository";
import { messagesRepository } from "../messages/messages.repository";
import { AppError } from "../../middleware/errorHandler";

export const conversationsRouter = Router();

conversationsRouter.get("/:id", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const conversation = await conversationsRepository.findById(
      tenantId,
      Number(req.params.id)
    );
    if (!conversation) {
      throw new AppError("NOT_FOUND", "Conversation not found.", 404);
    }
    res.json(conversation);
  } catch (err) {
    next(err);
  }
});

// Dashboard-facing message history read. Deliberately separate from
// the channel-facing POST /messages intake endpoint (messages.routes.ts) —
// this router is mounted under requireAuth (staff JWT) in app.ts, not
// requireApiKey, since a browser dashboard never holds the tenant's
// server-to-server api_key.
conversationsRouter.get(
  "/:id/messages",
  async (req: TenantRequest, res, next) => {
    try {
      const tenantId = req.tenantId!;
      const messages = await messagesRepository.listForConversation(
        tenantId,
        Number(req.params.id)
      );
      res.json(messages);
    } catch (err) {
      next(err);
    }
  }
);
