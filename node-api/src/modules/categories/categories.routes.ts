import { Router } from "express";
import { TenantRequest } from "../../middleware/tenant";
import { AppError } from "../../middleware/errorHandler";
import { categoriesRepository } from "./categories.repository";

export const categoriesRouter = Router();

categoriesRouter.get("/", async (req: TenantRequest, res, next) => {
  try {
    const categories = await categoriesRepository.list(req.tenantId!);
    res.json({ categories });
  } catch (err) {
    next(err);
  }
});

categoriesRouter.post("/", async (req: TenantRequest, res, next) => {
  try {
    const { name, slug, description, parentId } = req.body ?? {};
    if (!name || typeof name !== "string") {
      throw new AppError("VALIDATION_ERROR", "name is required.", 400);
    }

    if (parentId !== undefined && parentId !== null) {
      // Prevent a category pointing at a parent that doesn't exist (or
      // belongs to another tenant) -- the FK alone would 500 with a raw
      // Postgres error; this turns it into a clean 400.
      const parent = await categoriesRepository.findById(req.tenantId!, Number(parentId));
      if (!parent) {
        throw new AppError("INVALID_PARENT", "parentId does not refer to an existing category.", 400);
      }
    }

    const category = await categoriesRepository.create(req.tenantId!, {
      name,
      slug,
      description,
      parentId: parentId ?? null,
    });
    res.status(201).json(category);
  } catch (err) {
    // A duplicate (tenant_id, slug) violates the UNIQUE constraint
    // added in db/init/009_knowledge_customer_category.sql -- surfaced
    // as a clean 409 rather than a raw Postgres error leaking to the client.
    if (isUniqueViolation(err)) {
      return next(new AppError("DUPLICATE_SLUG", "A category with this slug already exists.", 409));
    }
    next(err);
  }
});

categoriesRouter.get("/:id", async (req: TenantRequest, res, next) => {
  try {
    const category = await categoriesRepository.findById(req.tenantId!, Number(req.params.id));
    if (!category) {
      throw new AppError("NOT_FOUND", "Category not found.", 404);
    }
    res.json(category);
  } catch (err) {
    next(err);
  }
});

categoriesRouter.patch("/:id", async (req: TenantRequest, res, next) => {
  try {
    const { name, slug, description, parentId } = req.body ?? {};
    if (parentId !== undefined && parentId !== null && Number(parentId) === Number(req.params.id)) {
      throw new AppError("INVALID_PARENT", "A category cannot be its own parent.", 400);
    }
    const updated = await categoriesRepository.update(req.tenantId!, Number(req.params.id), {
      name,
      slug,
      description,
      parentId,
    });
    if (!updated) {
      throw new AppError("NOT_FOUND", "Category not found.", 404);
    }
    res.json(updated);
  } catch (err) {
    if (isUniqueViolation(err)) {
      return next(new AppError("DUPLICATE_SLUG", "A category with this slug already exists.", 409));
    }
    next(err);
  }
});

categoriesRouter.delete("/:id", async (req: TenantRequest, res, next) => {
  try {
    // products.category_id and any child categories' parent_id both use
    // ON DELETE SET NULL (see the migration) -- deleting a category
    // never cascades into deleting products or grandchild categories,
    // it just un-categorizes them.
    const deleted = await categoriesRepository.delete(req.tenantId!, Number(req.params.id));
    if (!deleted) {
      throw new AppError("NOT_FOUND", "Category not found.", 404);
    }
    res.status(204).send();
  } catch (err) {
    next(err);
  }
});

function isUniqueViolation(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { code?: string }).code === "23505";
}
