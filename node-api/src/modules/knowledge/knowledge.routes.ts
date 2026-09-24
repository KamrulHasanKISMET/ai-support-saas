import { NextFunction, Response, Router } from "express";
import multer from "multer";
import { TenantRequest } from "../../middleware/tenant";
import { AppError } from "../../middleware/errorHandler";
import { env } from "../../config/env";
import { validateUpload } from "../../utils/fileValidation";
import { storage } from "../../utils/storage";
import { knowledgeRepository } from "./knowledge.repository";
import { ingestDocument, ingestFaq } from "./knowledge.client";
import { logger } from "../../utils/logger";

export const knowledgeRouter = Router();

// Buffered in memory (not disk) -- validateUpload needs the actual
// bytes for its magic-number check before anything is written to
// storage.ts, and MAX_UPLOAD_SIZE_BYTES (20MB default) keeps this
// bounded. multer enforces the size cap itself too, as a second line
// of defense before the buffer is even fully read.
const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: env.maxUploadSizeBytes },
});

/**
 * multer's own middleware calls next(err) with a raw `MulterError` on
 * things like an oversized file (LIMIT_FILE_SIZE) or an unexpected
 * field name -- left unwrapped, that would fall through errorHandler's
 * generic catch-all as an alarming, logged-as-error 500, rather than
 * the clean 400 an oversized upload actually is. This turns it into
 * the same AppError shape every other validation failure in this
 * route uses.
 */
function handleUpload(field: string) {
  const middleware = upload.single(field);
  return (req: TenantRequest, res: Response, next: NextFunction) => {
    middleware(req, res, (err: unknown) => {
      if (!err) return next();
      if (err instanceof multer.MulterError) {
        const message =
          err.code === "LIMIT_FILE_SIZE"
            ? `File exceeds the ${env.maxUploadSizeBytes}-byte limit.`
            : err.message;
        return next(new AppError("UPLOAD_ERROR", message, 400));
      }
      next(err);
    });
  };
}

const ALLOWED_CATEGORIES = [
  "General",
  "Products",
  "Pricing",
  "Delivery",
  "Return",
  "Refund",
  "Warranty",
  "FAQ",
  "Company Information",
  "Custom",
];

function validateCategory(category: unknown): string | undefined {
  if (category === undefined || category === null || category === "") return undefined;
  if (typeof category !== "string" || !ALLOWED_CATEGORIES.includes(category)) {
    throw new AppError(
      "INVALID_CATEGORY",
      `category must be one of: ${ALLOWED_CATEGORIES.join(", ")}`,
      400
    );
  }
  return category;
}

// ---------------------------------------------------------------------
// Documents
// ---------------------------------------------------------------------

knowledgeRouter.post("/documents", handleUpload("file"), async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    if (!req.file) {
      throw new AppError("MISSING_FILE", "A file is required (multipart field name: file).", 400);
    }
    const category = validateCategory(req.body?.category);
    const language = typeof req.body?.language === "string" ? req.body.language : undefined;

    // Phase 4: never trust the client-provided MIME type alone --
    // extension + declared Content-Type + magic-byte sniff must agree.
    const { format, sanitizedFilename } = validateUpload(
      req.file.originalname,
      req.file.mimetype,
      req.file.size,
      req.file.buffer,
      env.maxUploadSizeBytes
    );

    const storagePath = await storage.save(tenantId, sanitizedFilename, req.file.buffer);

    const document = await knowledgeRepository.createDocument(tenantId, {
      title: req.body?.title || sanitizedFilename,
      filename: sanitizedFilename,
      mimeType: req.file.mimetype,
      sizeBytes: req.file.size,
      storagePath,
      category,
      language,
    });

    res.status(201).json({ ...document, format });
  } catch (err) {
    next(err);
  }
});

knowledgeRouter.get("/documents", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const status = typeof req.query.status === "string" ? req.query.status : undefined;
    const category = typeof req.query.category === "string" ? req.query.category : undefined;
    const limit = Number(req.query.limit ?? 50);
    const offset = Number(req.query.offset ?? 0);
    if (!Number.isInteger(limit) || limit <= 0 || limit > 200) {
      throw new AppError("INVALID_LIMIT", "limit must be an integer between 1 and 200.", 400);
    }
    if (!Number.isInteger(offset) || offset < 0) {
      throw new AppError("INVALID_OFFSET", "offset must be a non-negative integer.", 400);
    }

    const { documents, total } = await knowledgeRepository.list(tenantId, {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      status: status as any,
      category,
      limit,
      offset,
    });
    res.json({ documents, total, limit, offset });
  } catch (err) {
    next(err);
  }
});

knowledgeRouter.get("/documents/:id", async (req: TenantRequest, res, next) => {
  try {
    const doc = await knowledgeRepository.findById(req.tenantId!, Number(req.params.id));
    if (!doc) throw new AppError("NOT_FOUND", "Document not found.", 404);
    res.json(doc);
  } catch (err) {
    next(err);
  }
});

/**
 * GET /knowledge/documents/:id/status -- phase 11. Deliberately a
 * small, safe projection of the full document row: no raw_content, no
 * storage_path (an internal detail, not something a frontend needs),
 * and never embeddings (which don't even live on this row -- they're
 * in knowledge_chunks, and this endpoint doesn't touch that table at
 * all, so there's nothing to accidentally leak).
 */
knowledgeRouter.get("/documents/:id/status", async (req: TenantRequest, res, next) => {
  try {
    const doc = await knowledgeRepository.findById(req.tenantId!, Number(req.params.id));
    if (!doc) throw new AppError("NOT_FOUND", "Document not found.", 404);
    res.json({
      id: doc.id,
      status: doc.status,
      chunks: doc.chunk_count,
      category: doc.category,
      error: doc.status === "FAILED" ? doc.error_message : undefined,
      processed_at: doc.processed_at,
    });
  } catch (err) {
    next(err);
  }
});

knowledgeRouter.delete("/documents/:id", async (req: TenantRequest, res, next) => {
  try {
    const deleted = await knowledgeRepository.deleteDocument(req.tenantId!, Number(req.params.id));
    if (!deleted) throw new AppError("NOT_FOUND", "Document not found.", 404);
    // Best-effort: an orphaned file on disk is a cleanup nuisance, not
    // a correctness problem (nothing references it anymore) -- delete
    // must not fail the request over a storage hiccup.
    if (deleted.storage_path) {
      try {
        await storage.delete(deleted.storage_path);
      } catch (storageErr) {
        logger.warn("knowledge_document_file_delete_failed", { error: String(storageErr) });
      }
    }
    res.status(204).send();
  } catch (err) {
    next(err);
  }
});

/**
 * POST /knowledge/documents/:id/process -- phase 5 pipeline trigger.
 * Synchronous (no job queue exists yet -- see
 * node-api/src/queues/README.md); the HTTP response only returns once
 * python-api has finished extracting/chunking/embedding, or failed.
 *
 * Idempotent: re-processing a document re-runs ingestion from scratch,
 * but python-api's ingestion_service.py deletes-then-reinserts that
 * document's chunks in one transaction rather than appending, so
 * repeated calls never accumulate duplicate chunks/embeddings (task
 * requirement, phase 5).
 */
knowledgeRouter.post("/documents/:id/process", async (req: TenantRequest, res, next) => {
  try {
    const tenantId = req.tenantId!;
    const documentId = Number(req.params.id);
    const doc = await knowledgeRepository.findById(tenantId, documentId);
    if (!doc) throw new AppError("NOT_FOUND", "Document not found.", 404);
    if (!doc.storage_path) {
      throw new AppError(
        "NO_FILE",
        "This document has no uploaded file to process (manually-entered content isn't supported by this endpoint yet).",
        400
      );
    }

    await knowledgeRepository.setStatus(tenantId, documentId, "PROCESSING");

    const fileBuffer = await storage.read(doc.storage_path);
    const result = await ingestDocument({
      tenantId,
      documentId,
      filename: doc.filename ?? "document",
      mimeType: doc.mime_type ?? "application/octet-stream",
      category: doc.category,
      language: doc.language,
      fileBuffer,
    });

    if (result.status === "FAILED") {
      await knowledgeRepository.setStatus(tenantId, documentId, "FAILED", result.errorMessage);
      const updated = await knowledgeRepository.findById(tenantId, documentId);
      // 200, not 500/502 -- this is a well-handled outcome the caller
      // can act on (retry, inspect error), not a server malfunction.
      return res.status(200).json(updated);
    }

    // python-api already wrote READY + chunk_count + processed_at
    // directly (it owns that part of the row -- see
    // ingestion_service.py); re-read to return the current row.
    const updated = await knowledgeRepository.findById(tenantId, documentId);
    res.json(updated);
  } catch (err) {
    next(err);
  }
});

// ---------------------------------------------------------------------
// FAQ
// ---------------------------------------------------------------------

knowledgeRouter.get("/faq", async (req: TenantRequest, res, next) => {
  try {
    const category = typeof req.query.category === "string" ? req.query.category : undefined;
    const faqs = await knowledgeRepository.listFaqs(req.tenantId!, category);
    res.json({ faqs });
  } catch (err) {
    next(err);
  }
});

knowledgeRouter.post("/faq", async (req: TenantRequest, res, next) => {
  try {
    const { question, answer, category } = req.body ?? {};
    if (!question || typeof question !== "string") {
      throw new AppError("VALIDATION_ERROR", "question is required.", 400);
    }
    if (!answer || typeof answer !== "string") {
      throw new AppError("VALIDATION_ERROR", "answer is required.", 400);
    }
    const validatedCategory = validateCategory(category);
    const tenantId = req.tenantId!;
    const faq = await knowledgeRepository.createFaq(tenantId, {
      question,
      answer,
      category: validatedCategory,
    });

    // Best-effort embedding (phase 9/10) -- a failure here logs and
    // leaves knowledge_document_id NULL; the FAQ itself is still saved
    // and visible via GET /knowledge/faq either way (task requirement:
    // don't let RAG availability block basic FAQ management).
    const knowledgeDocumentId = await ingestFaq({
      tenantId,
      faqId: faq.id,
      question,
      answer,
      category: validatedCategory,
    });
    if (knowledgeDocumentId) {
      await knowledgeRepository.setFaqKnowledgeDocumentId(tenantId, faq.id, knowledgeDocumentId);
      faq.knowledge_document_id = knowledgeDocumentId;
    }

    res.status(201).json(faq);
  } catch (err) {
    next(err);
  }
});

knowledgeRouter.patch("/faq/:id", async (req: TenantRequest, res, next) => {
  try {
    const { question, answer, category } = req.body ?? {};
    const validatedCategory = category !== undefined ? validateCategory(category) : undefined;
    const tenantId = req.tenantId!;
    const updated = await knowledgeRepository.updateFaq(tenantId, Number(req.params.id), {
      question,
      answer,
      category: validatedCategory,
    });
    if (!updated) throw new AppError("NOT_FOUND", "FAQ not found.", 404);

    // Re-embed with the merged (post-update) question/answer/category --
    // same best-effort behavior as create above.
    const knowledgeDocumentId = await ingestFaq({
      tenantId,
      faqId: updated.id,
      question: updated.question,
      answer: updated.answer,
      category: updated.category,
    });
    if (knowledgeDocumentId) {
      await knowledgeRepository.setFaqKnowledgeDocumentId(tenantId, updated.id, knowledgeDocumentId);
      updated.knowledge_document_id = knowledgeDocumentId;
    }

    res.json(updated);
  } catch (err) {
    next(err);
  }
});

knowledgeRouter.delete("/faq/:id", async (req: TenantRequest, res, next) => {
  try {
    const deleted = await knowledgeRepository.deleteFaq(req.tenantId!, Number(req.params.id));
    if (!deleted) throw new AppError("NOT_FOUND", "FAQ not found.", 404);
    res.status(204).send();
  } catch (err) {
    next(err);
  }
});
