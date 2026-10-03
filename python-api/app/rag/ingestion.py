"""
KNOWLEDGE INGESTION -- P6R-1 (docs/PHASE_5_8_PLAN.md, ROADMAP §2).

upload -> clean -> chunk -> embed -> store, for the tenant's OWN business
knowledge (catalog, policies, FAQs).

Guarantees:
* TENANT ISOLATION: every statement carries tenant_id.
* ALL-OR-NOTHING: chunks are embedded BEFORE any row is written.
* IDEMPOTENT: identical cleaned text for a tenant is skipped.
* BOUNDED: MAX_DOC_CHARS / MAX_CHUNKS cap cost per upload.
* SAFE SHAPE: each chunk is <= MAX_CHUNK_CHARS and embeddings must match
  the database vector dimension.

Not done here:
* file parsing (PDF/DOCX) -- callers send text
* no automatic re-embedding job when the embedding model changes
* no automatic PII scrubbing

Pure functions: clean_text, chunk_text, content_hash.
DB/embedding are injected.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.core.logging import logger

EMBEDDING_DIM = 1536
MAX_DOC_CHARS = 200_000
MAX_CHUNKS = 400
MAX_CHUNK_CHARS = 800
OVERLAP_CHARS = 120
MIN_CHUNK_CHARS = 20
EMBED_BATCH = 64

_SENT_SPLIT = re.compile(
    r"(?<=[.!?\u0964\u0965\u061F\u3002\uFF01\uFF1F])\s+"
)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZW = re.compile(r"[\u200b\u2060\ufeff]")


class IngestionError(ValueError):
    """Bad input (empty, too big, wrong embedding shape)."""


@dataclass(frozen=True)
class IngestResult:
    status: str
    document_id: int
    chunk_count: int


def clean_text(raw: str) -> str:
    """Normalize text without changing meaning."""
    t = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _CTRL.sub("", t)
    t = _ZW.sub("", t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def content_hash(cleaned: str) -> str:
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _hard_split(piece: str, limit: int) -> list[str]:
    """Split a sentence longer than limit on whitespace or raw characters."""
    out: list[str] = []
    cur = ""

    for word in piece.split(" "):
        while len(word) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(word[:limit])
            word = word[limit:]

        cand = word if not cur else f"{cur} {word}"
        if len(cand) <= limit:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = word

    if cur:
        out.append(cur)

    return out


def chunk_text(
    cleaned: str,
    max_chars: int = MAX_CHUNK_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> list[str]:
    """
    Deterministic, sentence-aware chunking.

    Paragraph breaks are preferred boundaries. A chunk never exceeds
    max_chars. Consecutive chunks share up to overlap chars where possible.
    Tiny chunks are merged into a neighbor and never silently dropped.
    """
    if max_chars < 50 or overlap < 0 or overlap >= max_chars // 2:
        raise IngestionError("bad chunk parameters")

    units: list[tuple[str, bool]] = []

    for para in [p.strip() for p in cleaned.split("\n\n") if p.strip()]:
        sents = [
            s.strip()
            for s in _SENT_SPLIT.split(para.replace("\n", " "))
            if s.strip()
        ]

        for i, sentence in enumerate(sents):
            body = max_chars - overlap
            pieces = (
                _hard_split(sentence, body)
                if len(sentence) > body
                else [sentence]
            )

            for j, piece in enumerate(pieces):
                units.append((piece, i == 0 and j == 0))

    chunks: list[str] = []
    cur = ""

    for sentence, new_para in units:
        candidate = sentence if not cur else f"{cur} {sentence}"

        if cur and (
            len(candidate) > max_chars
            or (new_para and len(cur) >= max_chars // 2)
        ):
            chunks.append(cur)

            last = _SENT_SPLIT.split(cur)[-1].strip()
            seed = (
                last
                if overlap and 0 < len(last) <= overlap and last != cur
                else ""
            )

            if (
                seed
                and len(seed) + 1 + len(sentence) <= max_chars
            ):
                cur = f"{seed} {sentence}".strip()
            else:
                cur = sentence
        else:
            cur = candidate

    if cur:
        chunks.append(cur)

    merged: list[str] = []
    for chunk in chunks:
        if (
            merged
            and len(chunk) < MIN_CHUNK_CHARS
            and len(merged[-1]) + 1 + len(chunk) <= max_chars
        ):
            merged[-1] = f"{merged[-1]} {chunk}"
        else:
            merged.append(chunk)

    return merged


async def _embed_all(embed_fn, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []

    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(await embed_fn(texts[i : i + EMBED_BATCH]))

    if len(out) != len(texts):
        raise IngestionError(
            "embedding provider returned the wrong number of vectors"
        )

    for vector in out:
        if len(vector) != EMBEDDING_DIM:
            raise IngestionError(
                f"embedding has {len(vector)} dims, "
                f"column needs {EMBEDDING_DIM}"
            )

    return out


async def ingest_document(
    db,
    *,
    tenant_id: int,
    title: str,
    content: str,
    embed_fn,
    embedding_model: str,
    source: str | None = None,
    language: str | None = None,
    category: str | None = None,
) -> IngestResult:
    """
    Ingest a tenant-owned knowledge document.

    Embeddings are generated before any write. Database writes are committed
    as one transaction and rolled back on failure.
    """
    from sqlalchemy import text

    if not isinstance(tenant_id, int) or tenant_id <= 0:
        raise IngestionError("tenant_id required")

    title = (title or "").strip()
    if not title or len(title) > 255:
        raise IngestionError("title required (max 255 chars)")

    if source is not None and len(source) > 255:
        raise IngestionError("source too long (max 255 chars)")

    cleaned = clean_text(content)

    if not cleaned:
        raise IngestionError("content is empty after cleaning")

    if len(cleaned) > MAX_DOC_CHARS:
        raise IngestionError(
            f"document too large ({len(cleaned)} > "
            f"{MAX_DOC_CHARS} chars); split it"
        )

    chunks = chunk_text(cleaned)

    if len(chunks) > MAX_CHUNKS:
        raise IngestionError(
            f"too many chunks ({len(chunks)} > {MAX_CHUNKS}); "
            "split the document"
        )

    digest = content_hash(cleaned)

    dup = (
        await db.execute(
            text(
                "SELECT id, chunk_count "
                "FROM knowledge_documents "
                "WHERE tenant_id = :t AND content_sha256 = :h"
            ),
            {"t": tenant_id, "h": digest},
        )
    ).first()

    if dup is not None:
        return IngestResult(
            "duplicate",
            int(dup.id),
            int(dup.chunk_count or 0),
        )

    old = None
    if source:
        old = (
            await db.execute(
                text(
                    "SELECT id FROM knowledge_documents "
                    "WHERE tenant_id = :t AND source = :s"
                ),
                {"t": tenant_id, "s": source},
            )
        ).first()

    vectors = await _embed_all(embed_fn, chunks)

    try:
        if old is not None:
            await db.execute(
                text(
                    "DELETE FROM knowledge_documents "
                    "WHERE id = :i AND tenant_id = :t"
                ),
                {"i": old.id, "t": tenant_id},
            )

        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO knowledge_documents
                        (
                            tenant_id,
                            title,
                            source,
                            language,
                            category,
                            raw_content,
                            content_sha256,
                            chunk_count,
                            embedding_model
                        )
                    VALUES
                        (
                            :t,
                            :title,
                            :source,
                            :lang,
                            :cat,
                            :raw,
                            :h,
                            :n,
                            :em
                        )
                    RETURNING id
                    """
                ),
                {
                    "t": tenant_id,
                    "title": title,
                    "source": source,
                    "lang": language,
                    "cat": category,
                    "raw": cleaned,
                    "h": digest,
                    "n": len(chunks),
                    "em": embedding_model,
                },
            )
        ).first()

        doc_id = int(row.id)

        for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
            await db.execute(
                text(
                    """
                    INSERT INTO knowledge_chunks
                        (
                            tenant_id,
                            document_id,
                            chunk_index,
                            content,
                            category,
                            language,
                            embedding
                        )
                    VALUES
                        (
                            :t,
                            :d,
                            :i,
                            :c,
                            :cat,
                            :lang,
                            CAST(:e AS vector)
                        )
                    """
                ),
                {
                    "t": tenant_id,
                    "d": doc_id,
                    "i": idx,
                    "c": chunk,
                    "cat": category,
                    "lang": language,
                    "e": str(vector),
                },
            )

        await db.commit()

    except Exception:
        await db.rollback()
        logger.error(
            "knowledge ingest failed tenant=%s (rolled back)",
            tenant_id,
            exc_info=True,
        )
        raise

    logger.info(
        "knowledge ingested tenant=%s doc=%s chunks=%d replaced=%s",
        tenant_id,
        doc_id,
        len(chunks),
        old is not None,
    )

    return IngestResult(
        "replaced" if old is not None else "created",
        doc_id,
        len(chunks),
    )


async def list_documents(
    db,
    tenant_id: int,
    limit: int = 100,
) -> list[dict]:
    """Return document metadata only for this tenant."""
    from sqlalchemy import text

    limit = max(1, min(int(limit), 500))

    result = await db.execute(
        text(
            """
            SELECT
                id,
                title,
                source,
                language,
                category,
                chunk_count,
                embedding_model,
                created_at
            FROM knowledge_documents
            WHERE tenant_id = :t
            ORDER BY id DESC
            LIMIT :n
            """
        ),
        {"t": tenant_id, "n": limit},
    )

    return [dict(row._mapping) for row in result]


async def delete_document(
    db,
    tenant_id: int,
    document_id: int,
) -> bool:
    """Delete only a document belonging to this tenant."""
    from sqlalchemy import text

    result = await db.execute(
        text(
            "DELETE FROM knowledge_documents "
            "WHERE id = :i AND tenant_id = :t"
        ),
        {"i": document_id, "t": tenant_id},
    )

    await db.commit()

    return (getattr(result, "rowcount", 0) or 0) > 0
