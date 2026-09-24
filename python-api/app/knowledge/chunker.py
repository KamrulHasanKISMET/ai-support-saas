"""
Deterministic chunking (CUSTOMER-KNOWLEDGE-RAG-API-001, phase 7).

Character-based sliding window with paragraph-aware boundaries where
possible -- doesn't rewrite/replace the existing RAG engine
(app/rag/search.py, hybrid_search.py, reranker.py all untouched); this
only produces the `content` strings that get embedded and inserted
into the existing knowledge_chunks table.

Deterministic: same input text + same size/overlap always produces the
same chunks, in the same order -- required for idempotent re-processing
(ingestion_service.py deletes and regenerates a document's chunks from
scratch on every /process call, so non-determinism here would just be
invisible, but determinism also makes this trivially unit-testable).
"""

from dataclasses import dataclass


@dataclass
class Chunk:
    index: int
    content: str


def chunk_text(
    text: str,
    chunk_size: int,
    overlap: int,
) -> list[Chunk]:
    """
    Splits on paragraph breaks first (keeps related sentences together
    when they fit), then greedily packs paragraphs into windows up to
    `chunk_size` characters. A single paragraph longer than chunk_size
    is hard-split with a sliding window of `overlap` characters shared
    between consecutive pieces, so no chunk ever exceeds chunk_size and
    no content is ever dropped.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and less than chunk_size")

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for para in paragraphs:
        if len(para) > chunk_size:
            # Paragraph itself is too big -- flush whatever's pending,
            # then hard-split this paragraph with overlap.
            flush()
            start = 0
            while start < len(para):
                end = min(start + chunk_size, len(para))
                chunks.append(para[start:end].strip())
                if end == len(para):
                    break
                start = end - overlap
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            flush()
            current = para

    flush()

    return [Chunk(index=i, content=c) for i, c in enumerate(chunks) if c]
