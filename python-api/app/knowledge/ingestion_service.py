"""
Ingestion pipeline orchestration (CUSTOMER-KNOWLEDGE-RAG-API-001, phase 5).

    UPLOADED -> PROCESSING -> EXTRACTED -> CHUNKED -> EMBEDDING -> READY
                                                                 -> FAILED

Node-api already flips UPLOADED -> PROCESSING before calling this
service (see node-api/src/modules/knowledge/knowledge.routes.ts); this
module owns every transition from there on, writing each one directly
to `knowledge_documents.status` as it happens so GET
/knowledge/documents/:id/status reflects real progress even if this
call is slow (no job queue exists yet -- the whole pipeline runs
synchronously inside one HTTP request, see
node-api/src/queues/README.md).

Idempotent by construction (task requirement): `ingest_document`
deletes this document's existing knowledge_chunks rows and reinserts
the freshly generated ones in the SAME transaction as the READY status
write. Re-running never leaves a partial mix of old and new chunks,
and never accumulates duplicates.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embedding_service import embedding_service
from app.core.config import settings
from app.core.logging import logger
from app.knowledge.chunker import chunk_text
from app.knowledge.extractors import ExtractionError, extract_text


class IngestionResult:
    def __init__(self, status: str, chunk_count: int, error_message: str | None = None):
        self.status = status
        self.chunk_count = chunk_count
        self.error_message = error_message


async def _set_status(
    db: AsyncSession, tenant_id: int, document_id: int, status: str, **extra
) -> None:
    """Writes a status transition immediately (own commit) so a
    slow/failing later step doesn't hide earlier progress from GET
    .../status. Deliberately NOT part of the final ingest transaction."""
    set_clauses = ["status = :status", "updated_at = now()"]
    params = {"status": status, "tenant_id": tenant_id, "document_id": document_id}
    for key, value in extra.items():
        set_clauses.append(f"{key} = :{key}")
        params[key] = value
    await db.execute(
        text(
            f"UPDATE knowledge_documents SET {', '.join(set_clauses)} "
            "WHERE tenant_id = :tenant_id AND id = :document_id"
        ),
        params,
    )
    await db.commit()


def _format_from_filename(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("pdf", "docx", "txt", "csv"):
        raise ExtractionError(f'Unsupported file extension ".{ext}".')
    return ext


async def ingest_document(
    db: AsyncSession,
    *,
    tenant_id: int,
    document_id: int,
    filename: str,
    file_bytes: bytes,
    category: str | None,
    language: str | None,
) -> IngestionResult:
    try:
        fmt = _format_from_filename(filename)

        # EXTRACTED
        raw_text = extract_text(fmt, file_bytes)
        await _set_status(db, tenant_id, document_id, "EXTRACTED", raw_content=raw_text[:100_000])
        # ^ raw_content capped at 100k chars -- a full-fidelity copy of
        # very large source documents isn't needed once chunks exist;
        # this column exists mainly for debugging/re-chunking without
        # re-uploading, not as the canonical store (the file itself,
        # via storage_path on node-api, remains that).

        # CHUNKED
        chunks = chunk_text(
            raw_text,
            chunk_size=settings.knowledge_chunk_size_chars,
            overlap=settings.knowledge_chunk_overlap_chars,
        )
        if not chunks:
            raise ExtractionError("No content could be chunked from this document.")
        await _set_status(db, tenant_id, document_id, "CHUNKED")

        # EMBEDDING
        await _set_status(db, tenant_id, document_id, "EMBEDDING")
        vectors = await embedding_service.embed_batch([c.content for c in chunks])

        # Store -- delete+reinsert chunks and flip to READY together,
        # so a crash between them can never leave READY with zero/stale
        # chunks, or old chunks with no matching READY status.
        await db.execute(
            text("DELETE FROM knowledge_chunks WHERE tenant_id = :tenant_id AND document_id = :document_id"),
            {"tenant_id": tenant_id, "document_id": document_id},
        )

        for chunk, vector in zip(chunks, vectors):
            await db.execute(
                text(
                    """
                    INSERT INTO knowledge_chunks
                        (tenant_id, document_id, chunk_index, content, category, language, source, embedding)
                    VALUES
                        (:tenant_id, :document_id, :chunk_index, :content, :category, :language, :source, :embedding)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "chunk_index": chunk.index,
                    "content": chunk.content,
                    "category": category,
                    "language": language or "bn",
                    "source": filename,
                    "embedding": str(vector),
                },
            )

        await db.execute(
            text(
                """
                UPDATE knowledge_documents
                   SET status = 'READY', chunk_count = :chunk_count,
                       processed_at = now(), error_message = NULL, updated_at = now()
                 WHERE tenant_id = :tenant_id AND id = :document_id
                """
            ),
            {"chunk_count": len(chunks), "tenant_id": tenant_id, "document_id": document_id},
        )
        await db.commit()

        logger.info(
            "knowledge_ingestion_succeeded",
            extra={"tenant_id": tenant_id, "document_id": document_id, "chunk_count": len(chunks)},
        )
        return IngestionResult(status="READY", chunk_count=len(chunks))

    except ExtractionError as exc:
        await db.rollback()
        await _set_status(db, tenant_id, document_id, "FAILED", error_message=str(exc))
        logger.info(
            "knowledge_ingestion_failed",
            extra={"tenant_id": tenant_id, "document_id": document_id, "error": str(exc)},
        )
        return IngestionResult(status="FAILED", chunk_count=0, error_message=str(exc))

    except Exception as exc:  # noqa: BLE001
        # Unexpected error (embedding API down, DB error, etc.) --
        # never leak the raw exception message to the client (task
        # phase 5: "without exposing sensitive internals to normal
        # clients"); log the real one for ops.
        await db.rollback()
        safe_message = "Processing failed due to an internal error. Please try again."
        await _set_status(db, tenant_id, document_id, "FAILED", error_message=safe_message)
        logger.error(
            "knowledge_ingestion_unexpected_error",
            extra={"tenant_id": tenant_id, "document_id": document_id, "error": str(exc)},
        )
        return IngestionResult(status="FAILED", chunk_count=0, error_message=safe_message)


async def upsert_faq_chunk(
    db: AsyncSession,
    *,
    tenant_id: int,
    faq_id: int,
    question: str,
    answer: str,
    category: str | None,
) -> int:
    """
    Embeds a single FAQ as one chunk, via a per-FAQ synthetic
    knowledge_documents row (so it's visible through the same
    GET /knowledge/documents listing/RAG search path as uploaded
    documents, without a second, parallel storage mechanism).

    Idempotent the same way ingest_document is: finds-or-creates the
    synthetic document by a stable title ("FAQ #<id>"), then
    delete+reinserts its one chunk. Returns the knowledge_document_id
    for the caller (node-api) to store on knowledge_faqs.knowledge_document_id.
    """
    title = f"FAQ #{faq_id}"
    existing = await db.execute(
        text(
            "SELECT id FROM knowledge_documents WHERE tenant_id = :tenant_id AND title = :title"
        ),
        {"tenant_id": tenant_id, "title": title},
    )
    row = existing.first()
    content = f"Q: {question}\nA: {answer}"

    if row:
        document_id = row[0]
        await db.execute(
            text(
                "UPDATE knowledge_documents SET raw_content = :content, category = :category, "
                "status = 'EMBEDDING', updated_at = now() WHERE id = :id"
            ),
            {"content": content, "category": category, "id": document_id},
        )
    else:
        result = await db.execute(
            text(
                """
                INSERT INTO knowledge_documents (tenant_id, title, source, category, raw_content, status)
                VALUES (:tenant_id, :title, 'faq', :category, :content, 'EMBEDDING')
                RETURNING id
                """
            ),
            {"tenant_id": tenant_id, "title": title, "category": category, "content": content},
        )
        document_id = result.scalar_one()

    await db.execute(
        text("DELETE FROM knowledge_chunks WHERE tenant_id = :tenant_id AND document_id = :document_id"),
        {"tenant_id": tenant_id, "document_id": document_id},
    )

    [vector] = await embedding_service.embed_batch([content])
    await db.execute(
        text(
            """
            INSERT INTO knowledge_chunks
                (tenant_id, document_id, chunk_index, content, category, language, source, embedding)
            VALUES (:tenant_id, :document_id, 0, :content, :category, 'bn', 'faq', :embedding)
            """
        ),
        {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "content": content,
            "category": category,
            "embedding": str(vector),
        },
    )
    await db.execute(
        text(
            "UPDATE knowledge_documents SET status = 'READY', chunk_count = 1, "
            "processed_at = now(), updated_at = now() WHERE id = :id"
        ),
        {"id": document_id},
    )
    await db.commit()
    return document_id
