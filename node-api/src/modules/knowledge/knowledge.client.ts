import axios from "axios";
import FormData from "form-data";
import { env } from "../../config/env";
import { logger } from "../../utils/logger";

export interface IngestResult {
  status: "READY" | "FAILED";
  chunkCount: number;
  errorMessage?: string;
}
const pythonApi = axios.create({
  baseURL: env.pythonApiUrl,
  timeout: 120_000, // extraction + embedding of a large document can genuinely take a while;
  // no queue exists yet for this (see node-api/src/queues/README.md), so this
  // request is synchronous end-to-end -- see docs/API_CONTRACTS.md's note on
  // POST /knowledge/documents/:id/process.
});

/**
 * Node never does text extraction/chunking/embedding itself (same
 * "Node hands heavy AI work to Python" rule as ai.client.ts's
 * runKernel). Sends the already-validated file bytes (Node already
 * has them via storage.ts) straight through -- python-api never needs
 * filesystem access to node-api's upload volume.
 */
export async function ingestDocument(params: {
  tenantId: number;
  documentId: number;
  filename: string;
  mimeType: string;
  category: string | null;
  language: string | null;
  fileBuffer: Buffer;
}): Promise<IngestResult> {
  const form = new FormData();
  form.append("tenantId", String(params.tenantId));
  form.append("documentId", String(params.documentId));
  if (params.category) form.append("category", params.category);
  if (params.language) form.append("language", params.language);
  form.append("file", params.fileBuffer, {
    filename: params.filename,
    contentType: params.mimeType,
  });

  try {
    const { data } = await pythonApi.post<IngestResult>("/knowledge/ingest", form, {
      headers: {
        ...form.getHeaders(),
        "x-internal-secret": env.internalServiceSecret,
      },
      maxBodyLength: env.maxUploadSizeBytes + 1024, // allow the multipart envelope's small overhead
    });
    return data;
  } catch (err) {
    // Controlled failure, same principle as runKernel(): the caller
    // (knowledge.routes.ts) still needs to mark the document FAILED
    // and return a clean response, never a raw 500 from an axios error
    // leaking connection details to the client.
    // Matches ai.client.ts's runKernel() convention -- plain
    // instanceof check rather than axios.isAxiosError, which needs
    // axios's own types to narrow `err` from `unknown` correctly.
    const message = err instanceof Error ? err.message : String(err);
    logger.error("knowledge_ingest_call_failed", {
      tenantId: params.tenantId,
      documentId: params.documentId,
      error: message,
    });
    return { status: "FAILED", chunkCount: 0, errorMessage: "Processing failed. Please try again." };
  }
}

/**
 * Embeds an FAQ Q&A pair via python-api's synthetic-document path
 * (phase 9/10 -- FAQs are retrievable by RAG search the same way
 * uploaded documents are). Called right after a FAQ is created/edited
 * (knowledge.routes.ts); returns null on failure so the FAQ write
 * itself still succeeds -- embedding is best-effort/retryable, not
 * something that should roll back a successful FAQ save.
 */
export async function ingestFaq(params: {
  tenantId: number;
  faqId: number;
  question: string;
  answer: string;
  category?: string | null;
}): Promise<number | null> {
  const form = new FormData();
  form.append("tenantId", String(params.tenantId));
  form.append("faqId", String(params.faqId));
  form.append("question", params.question);
  form.append("answer", params.answer);
  if (params.category) form.append("category", params.category);

  try {
    const { data } = await pythonApi.post<{ knowledgeDocumentId: number }>(
      "/knowledge/faq/ingest",
      form,
      {
        headers: {
          ...form.getHeaders(),
          "x-internal-secret": env.internalServiceSecret,
        },
      }
    );
    return data.knowledgeDocumentId;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    logger.error("knowledge_faq_ingest_call_failed", {
      tenantId: params.tenantId,
      faqId: params.faqId,
      error: message,
    });
    return null;
  }
}
