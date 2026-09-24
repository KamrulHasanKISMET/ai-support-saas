import { Router } from "express";
import { TenantRequest } from "../../middleware/tenant";
import { customersRepository } from "./customers.repository";
import { AppError } from "../../middleware/errorHandler";

export const customersRouter = Router();

/**
 * GET /customers?search=&limit=&offset=
 * Dashboard list/search (CUSTOMER-KNOWLEDGE-RAG-API-001 phase 1).
 * Registered BEFORE GET /:id so a request for the list never matches
 * the :id route.
 */
customersRouter.get("/", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const search = typeof req.query.search === "string" ? req.query.search.trim() : undefined;

    const limitRaw = Number(req.query.limit ?? 50);
    const offsetRaw = Number(req.query.offset ?? 0);
    if (!Number.isInteger(limitRaw) || limitRaw <= 0 || limitRaw > 200) {
      throw new AppError("INVALID_LIMIT", "limit must be an integer between 1 and 200.", 400);
    }
    if (!Number.isInteger(offsetRaw) || offsetRaw < 0) {
      throw new AppError("INVALID_OFFSET", "offset must be a non-negative integer.", 400);
    }

    const { customers, total } = await customersRepository.list(tenantId, {
      search: search || undefined,
      limit: limitRaw,
      offset: offsetRaw,
    });
    res.json({ customers, total, limit: limitRaw, offset: offsetRaw });
  } catch (err) {
    next(err);
  }
});

customersRouter.post("/", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const customer = await customersRepository.create(tenantId, req.body);
    res.status(201).json(customer);
  } catch (err) {
    next(err);
  }
});

customersRouter.get("/:id", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const customer = await customersRepository.findById(tenantId, Number(req.params.id));
    if (!customer) {
      throw new AppError("NOT_FOUND", "Customer not found.", 404);
    }
    res.json(customer);
  } catch (err) {
    next(err);
  }
});

customersRouter.patch("/:id", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const updated = await customersRepository.update(tenantId, Number(req.params.id), req.body);
    if (!updated) {
      // Tenant-scoped by construction (repository's WHERE tenant_id = $1
      // AND id = $2) -- another tenant's customer id 404s exactly like a
      // nonexistent one, never leaking whether the id exists elsewhere.
      throw new AppError("NOT_FOUND", "Customer not found.", 404);
    }
    res.json(updated);
  } catch (err) {
    next(err);
  }
});

customersRouter.delete("/:id", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const deleted = await customersRepository.delete(tenantId, Number(req.params.id));
    if (!deleted) {
      throw new AppError("NOT_FOUND", "Customer not found.", 404);
    }
    res.status(204).send();
  } catch (err) {
    next(err);
  }
});
