import { Router } from "express";
import { TenantRequest } from "../../middleware/tenant";
import { AppError } from "../../middleware/errorHandler";
import { channelCredentialsRepository } from "./channel_credentials.repository";

export const channelsRouter = Router();

// Dashboard-facing (mounted under requireAuth in app.ts) -- staff
// connect/view their own tenant's channel accounts here. The actual
// inbound webhook Meta calls is a SEPARATE, unauthenticated-by-us
// route (its own signature check instead) -- see
// whatsapp.webhook.ts, mounted at /webhooks/whatsapp in app.ts.

channelsRouter.get("/", (_req, res) => {
  res.json({
    status: "ok",
    channels: ["whatsapp"],
    todo: ["facebook"],
  });
});

channelsRouter.get("/whatsapp/credentials", async (req: TenantRequest, res, next) => {
  try {
    const credential = await channelCredentialsRepository.findByTenantAndChannel(
      req.tenantId!,
      "whatsapp"
    );
    if (!credential) {
      return res.json({ connected: false });
    }
    // Never return access_token back to the dashboard once stored.
    res.json({
      connected: true,
      externalAccountId: credential.external_account_id,
      isActive: credential.is_active,
    });
  } catch (err) {
    next(err);
  }
});

/**
 * Connects (or reconnects/rotates the token for) this tenant's
 * WhatsApp Business phone number. `phoneNumberId` and `accessToken`
 * come from the tenant's own Meta App/Business setup — this endpoint
 * only stores them, it does not create or verify them with Meta.
 */
channelsRouter.post("/whatsapp/credentials", async (req: TenantRequest, res, next) => {
  try {
    const { phoneNumberId, accessToken } = req.body as {
      phoneNumberId?: string;
      accessToken?: string;
    };
    if (!phoneNumberId || !accessToken) {
      throw new AppError(
        "VALIDATION_ERROR",
        "phoneNumberId and accessToken are required."
      );
    }

    await channelCredentialsRepository.upsert(
      req.tenantId!,
      "whatsapp",
      phoneNumberId,
      accessToken
    );
    res.status(201).json({ connected: true, externalAccountId: phoneNumberId });
  } catch (err) {
    if ((err as { code?: string }).code === "23505") {
      return next(
        new AppError(
          "CONFLICT",
          "This WhatsApp phone number is already connected to a different tenant.",
          409
        )
      );
    }
    next(err);
  }
});
