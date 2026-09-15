import { Router } from "express";
import { tenantsRepository } from "./tenants.repository";
import { AppError } from "../../middleware/errorHandler";

export const tenantsRouter = Router();

// NOTE: tenant creation is a platform-admin operation and does NOT go
// through the resolveTenant middleware (there is no tenant yet).
tenantsRouter.post("/", async (req, res, next) => {
  try {
    const { name, slug } = req.body;
    if (!name || !slug) {
      throw new AppError("VALIDATION_ERROR", "name and slug are required.");
    }
    const tenant = await tenantsRepository.create(name, slug);
    res.status(201).json(tenant);
  } catch (err) {
    next(err);
  }
});

tenantsRouter.get("/:slug", async (req, res, next) => {
  try {
    const tenant = await tenantsRepository.findBySlug(req.params.slug);
    if (!tenant) {
      throw new AppError("NOT_FOUND", "Tenant not found.", 404);
    }
    // This endpoint has no auth (there's no tenant context yet to check
    // against) — never return api_key here, or anyone who guesses a
    // slug could steal that tenant's server-to-server secret.
    const { api_key: _apiKey, ...publicTenant } = tenant;
    res.json(publicTenant);
  } catch (err) {
    next(err);
  }
});
