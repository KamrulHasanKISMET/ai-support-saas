from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_internal_secret
from app.knowledge.ingestion_service import ingest_document, upsert_faq_chunk
from app.schemas.knowledge import IngestResponse

router = APIRouter(
    prefix="/knowledge", tags=["knowledge"], dependencies=[Depends(require_internal_secret)]
)


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    tenantId: int = Form(...),
    documentId: int = Form(...),
    category: str | None = Form(default=None),
    language: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """
    Called by node-api's knowledge.client.ts (POST /documents/:id/process)
    with the already-validated file bytes -- node-api already ran
    fileValidation.ts's extension/MIME/magic-byte checks and owns the
    tenant-auth/ownership check (a caller here has already proven it
    holds INTERNAL_SERVICE_SECRET, but tenant_id/document_id are still
    just data at this layer, not re-authorized -- node-api is the only
    caller and already scoped them to the authenticated tenant).

    Runs the full extract -> chunk -> embed -> store pipeline
    synchronously and returns once it's done (see
    ingestion_service.py's module docstring for why: no job queue
    exists yet).
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
        status=result.status, chunkCount=result.chunk_count, errorMessage=result.error_message
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
    """Called by node-api after POST/PATCH /knowledge/faq -- embeds the
    Q&A pair as a single chunk so it's retrievable by RAG search
    alongside uploaded documents (phase 9/10). Returns the synthetic
    knowledge_documents.id node-api stores on
    knowledge_faqs.knowledge_document_id."""
    document_id = await upsert_faq_chunk(
        db,
        tenant_id=tenantId,
        faq_id=faqId,
        question=question,
        answer=answer,
        category=category,
    )
    return {"knowledgeDocumentId": document_id}
