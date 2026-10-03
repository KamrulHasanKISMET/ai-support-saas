from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_internal_secret
from app.knowledge.ingestion_service import (
    ingest_document,
    upsert_faq_chunk,
)
from app.rag.ingestion import (
    delete_document,
    list_documents,
)
from app.schemas.knowledge import IngestResponse

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    dependencies=[Depends(require_internal_secret)],
)


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    tenantId: int = Form(..., alias="tenantId", extra="forbid"),
    documentId: int = Form(...),
    category: str | None = Form(default=None),
    language: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """
    Called by node-api's knowledge.client.ts (POST /documents/:id/process)
    with the already-validated file bytes.

    Runs the existing file-based extract -> chunk -> embed -> store
    pipeline synchronously.
    """
    file_bytes = await file.read()

    result = await ingest_document(
        db,
        tenant_id=tenantId,
        document_id=documentId,
        filename=file.filename or "document",
        file_bytes=file_bytes,
        category=category,
        language=language,
    )

    return IngestResponse(
        status=result.status,
        chunkCount=result.chunk_count,
        errorMessage=result.error_message,
    )


@router.post("/faq/ingest")
async def ingest_faq(
    tenantId: int = Form(...),
    faqId: int = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
    category: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Called by node-api after POST/PATCH /knowledge/faq.

    Embeds the Q&A pair as a single chunk so it is retrievable by RAG.
    """
    document_id = await upsert_faq_chunk(
        db,
        tenant_id=tenantId,
        faq_id=faqId,
        question=question,
        answer=answer,
        category=category,
    )

    return {"knowledgeDocumentId": document_id}


@router.get("/documents")
async def get_documents(
    tenantId: int = Form(..., alias="tenantId", extra="forbid"),
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """
    Return metadata for knowledge documents belonging to this tenant.

    Raw document content is intentionally excluded.
    """
    return await list_documents(
        db,
        tenant_id=tenantId,
        limit=limit,
    )


@router.delete("/documents/{document_id}")
async def remove_document(
    document_id: int,
    tenantId: int = Form(..., alias="tenantId", extra="forbid"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Delete a knowledge document only when it belongs to this tenant.
    knowledge_chunks are removed by the database foreign-key cascade.
    """
    deleted = await delete_document(
        db,
        tenant_id=tenantId,
        document_id=document_id,
    )
    return {
        "deleted": deleted,
        "documentId": document_id,
    }