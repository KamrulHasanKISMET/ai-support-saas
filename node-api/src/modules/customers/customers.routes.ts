import { Router } from "express";
import { TenantRequest } from "../../middleware/tenant";
import { customersRepository } from "./customers.repository";
import { AppError } from "../../middleware/errorHandler";

export const customersRouter = Router();

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
