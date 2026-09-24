"""
Text extraction (CUSTOMER-KNOWLEDGE-RAG-API-001, phase 6).

One `DocumentExtractor` interface, one implementation per supported
format. Routes (api/routes/knowledge.py) never touch these directly --
only ingestion_service.py calls extract_text(), per the task's "do not
put extraction logic directly inside route handlers" instruction.

Every extractor raises `ExtractionError` (never a raw library
exception) on failure, so ingestion_service.py has exactly one
exception type to catch and turn into a safe, displayable
`error_message` on the document row (task phase 5: "store useful
processing error information without exposing sensitive internals").
"""

import csv
import io

from docx import Document as DocxDocument
from pypdf import PdfReader
from pypdf.errors import PdfReadError


class ExtractionError(Exception):
    """Safe-to-display message only -- never wraps/leaks a raw library
    traceback or internal file path."""


class DocumentExtractor:
    def extract(self, data: bytes) -> str:
        raise NotImplementedError


class PDFExtractor(DocumentExtractor):
    def extract(self, data: bytes) -> str:
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                # pypdf can sometimes still read a blank-password-encrypted
                # PDF; try once before giving up rather than always failing.
                try:
                    reader.decrypt("")
                except Exception:
                    raise ExtractionError("This PDF is password-protected and cannot be read.")
            pages = [page.extract_text() or "" for page in reader.pages]
        except PdfReadError as exc:
            raise ExtractionError("This file is not a valid or readable PDF.") from exc
        text = "\n\n".join(p.strip() for p in pages if p.strip())
        if not text:
            raise ExtractionError(
                "No extractable text was found in this PDF (it may be scanned "
                "images without OCR, which isn't supported yet)."
            )
        return text


class DOCXExtractor(DocumentExtractor):
    def extract(self, data: bytes) -> str:
        try:
            doc = DocxDocument(io.BytesIO(data))
        except Exception as exc:
            raise ExtractionError("This file is not a valid or readable DOCX document.") from exc

        parts: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]
        # Table text matters for pricing/spec sheets uploaded as tables
        # -- skipping tables would silently drop real content.
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))

        text = "\n\n".join(parts)
        if not text.strip():
            raise ExtractionError("No extractable text was found in this DOCX document.")
        return text


class TXTExtractor(DocumentExtractor):
    def extract(self, data: bytes) -> str:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExtractionError("This file is not valid UTF-8 text.") from exc
        if not text.strip():
            raise ExtractionError("This text file is empty.")
        return text


class CSVExtractor(DocumentExtractor):
    """Flattens rows into readable lines ("col1: val1 | col2: val2")
    rather than passing raw CSV through -- raw comma-separated text
    chunks and embeds poorly (no column context per cell)."""

    def extract(self, data: bytes) -> str:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExtractionError("This file is not valid UTF-8 text.") from exc

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            raise ExtractionError("This CSV file is empty.")

        header, *data_rows = rows
        lines: list[str] = []
        for row in data_rows:
            if not any(cell.strip() for cell in row):
                continue
            pairs = [
                f"{h.strip()}: {v.strip()}"
                for h, v in zip(header, row)
                if v.strip()
            ]
            if pairs:
                lines.append(" | ".join(pairs))

        if not lines:
            raise ExtractionError("No data rows were found in this CSV file.")
        return "\n".join(lines)


_EXTRACTORS: dict[str, DocumentExtractor] = {
    "pdf": PDFExtractor(),
    "docx": DOCXExtractor(),
    "txt": TXTExtractor(),
    "csv": CSVExtractor(),
}


def extract_text(fmt: str, data: bytes) -> str:
    """fmt is one of "pdf"/"docx"/"txt"/"csv" -- already validated by
    node-api's utils/fileValidation.ts before the file ever reaches
    this service; an unknown value here means a bug in the caller, not
    a user-facing validation case, hence a plain ValueError rather than
    ExtractionError."""
    extractor = _EXTRACTORS.get(fmt)
    if extractor is None:
        raise ValueError(f"Unsupported format: {fmt}")
    return extractor.extract(data)
