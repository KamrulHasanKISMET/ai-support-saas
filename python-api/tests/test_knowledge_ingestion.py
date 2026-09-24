import unittest
from unittest.mock import AsyncMock, patch

from app.knowledge import ingestion_service
from app.knowledge.extractors import ExtractionError


def _make_db_mock():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestIngestDocument(unittest.IsolatedAsyncioTestCase):
    async def test_successful_ingestion_writes_ready_status_with_chunk_count(self):
        db = _make_db_mock()
        with patch(
            "app.knowledge.ingestion_service.extract_text",
            return_value="Paragraph one.\n\nParagraph two.",
        ), patch(
            "app.knowledge.ingestion_service.embedding_service.embed_batch",
            new=AsyncMock(return_value=[[0.1, 0.2], [0.3, 0.4]]),
        ):
            result = await ingestion_service.ingest_document(
                db,
                tenant_id=1,
                document_id=42,
                filename="policy.txt",
                file_bytes=b"irrelevant, extract_text is mocked",
                category="Policy",
                language="bn",
            )

        self.assertEqual(result.status, "READY")
        self.assertGreaterEqual(result.chunk_count, 1)
        db.commit.assert_awaited()

    async def test_every_write_is_scoped_to_the_calling_tenant_id(self):
        """Tenant isolation at the write layer: every SQL statement this
        function executes must filter/insert with the SAME tenant_id
        the caller passed -- never a hardcoded or different one."""
        db = _make_db_mock()
        with patch(
            "app.knowledge.ingestion_service.extract_text", return_value="Some content here."
        ), patch(
            "app.knowledge.ingestion_service.embedding_service.embed_batch",
            new=AsyncMock(return_value=[[0.1, 0.2]]),
        ):
            await ingestion_service.ingest_document(
                db,
                tenant_id=7,
                document_id=99,
                filename="notes.txt",
                file_bytes=b"x",
                category=None,
                language=None,
            )

        for call in db.execute.call_args_list:
            params = call.args[1] if len(call.args) > 1 else {}
            if "tenant_id" in params:
                self.assertEqual(params["tenant_id"], 7)

    async def test_reprocessing_deletes_existing_chunks_before_reinserting(self):
        """Idempotency (task requirement): repeated processing must not
        accumulate duplicate chunks. Verified here at the query level --
        a DELETE for this tenant_id/document_id must occur before any
        chunk INSERT."""
        db = _make_db_mock()
        with patch(
            "app.knowledge.ingestion_service.extract_text",
            return_value="Paragraph one.\n\nParagraph two.",
        ), patch(
            "app.knowledge.ingestion_service.embedding_service.embed_batch",
            new=AsyncMock(return_value=[[0.1], [0.2]]),
        ):
            await ingestion_service.ingest_document(
                db,
                tenant_id=1,
                document_id=5,
                filename="a.txt",
                file_bytes=b"x",
                category=None,
                language=None,
            )

        statements = [call.args[0].text if hasattr(call.args[0], "text") else str(call.args[0])
                      for call in db.execute.call_args_list]
        delete_indices = [i for i, s in enumerate(statements) if "DELETE FROM knowledge_chunks" in s]
        insert_indices = [i for i, s in enumerate(statements) if "INSERT INTO knowledge_chunks" in s]

        self.assertTrue(delete_indices, "expected a DELETE FROM knowledge_chunks statement")
        self.assertTrue(insert_indices, "expected at least one INSERT INTO knowledge_chunks statement")
        self.assertLess(
            delete_indices[0], min(insert_indices), "DELETE must happen before any chunk INSERT"
        )

    async def test_extraction_failure_sets_failed_status_with_safe_message(self):
        db = _make_db_mock()
        with patch(
            "app.knowledge.ingestion_service.extract_text",
            side_effect=ExtractionError("No extractable text found in PDF."),
        ):
            result = await ingestion_service.ingest_document(
                db,
                tenant_id=1,
                document_id=1,
                filename="scan.pdf",
                file_bytes=b"x",
                category=None,
                language=None,
            )

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.chunk_count, 0)
        self.assertIn("No extractable text", result.error_message)
        db.rollback.assert_awaited()

    async def test_unexpected_error_never_leaks_raw_exception_to_caller(self):
        """Task phase 5: 'store useful processing error information
        without exposing sensitive internals to normal clients.'"""
        db = _make_db_mock()
        with patch(
            "app.knowledge.ingestion_service.extract_text",
            side_effect=RuntimeError("connection to internal-db-host-10.2.3.4 refused"),
        ):
            result = await ingestion_service.ingest_document(
                db,
                tenant_id=1,
                document_id=1,
                filename="a.txt",
                file_bytes=b"x",
                category=None,
                language=None,
            )

        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("internal-db-host", result.error_message)
        self.assertNotIn("10.2.3.4", result.error_message)

    async def test_unsupported_extension_fails_cleanly(self):
        db = _make_db_mock()
        result = await ingestion_service.ingest_document(
            db,
            tenant_id=1,
            document_id=1,
            filename="malware.exe",
            file_bytes=b"x",
            category=None,
            language=None,
        )
        self.assertEqual(result.status, "FAILED")

    async def test_no_chunks_produced_is_treated_as_failure_not_silent_success(self):
        db = _make_db_mock()
        with patch("app.knowledge.ingestion_service.extract_text", return_value="   "), patch(
            "app.knowledge.ingestion_service.chunk_text", return_value=[]
        ):
            result = await ingestion_service.ingest_document(
                db,
                tenant_id=1,
                document_id=1,
                filename="a.txt",
                file_bytes=b"x",
                category=None,
                language=None,
            )
        self.assertEqual(result.status, "FAILED")


class TestUpsertFaqChunk(unittest.IsolatedAsyncioTestCase):
    async def test_creates_a_new_synthetic_document_on_first_call(self):
        db = _make_db_mock()
        # First SELECT (find existing) -> no row; INSERT -> returns new id.
        select_result = AsyncMock()
        select_result.first = lambda: None
        insert_result = AsyncMock()
        insert_result.scalar_one = lambda: 123
        db.execute.side_effect = [select_result, insert_result, AsyncMock(), AsyncMock(), AsyncMock()]

        with patch(
            "app.knowledge.ingestion_service.embedding_service.embed_batch",
            new=AsyncMock(return_value=[[0.1, 0.2]]),
        ):
            document_id = await ingestion_service.upsert_faq_chunk(
                db,
                tenant_id=3,
                faq_id=10,
                question="What is your return policy?",
                answer="30 days, no questions asked.",
                category="Return",
            )

        self.assertEqual(document_id, 123)
        db.commit.assert_awaited()

    async def test_reuses_existing_synthetic_document_on_edit(self):
        db = _make_db_mock()
        select_result = AsyncMock()
        select_result.first = lambda: (55,)  # existing document id
        db.execute.side_effect = [select_result, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()]

        with patch(
            "app.knowledge.ingestion_service.embedding_service.embed_batch",
            new=AsyncMock(return_value=[[0.5, 0.6]]),
        ):
            document_id = await ingestion_service.upsert_faq_chunk(
                db,
                tenant_id=3,
                faq_id=10,
                question="Updated question?",
                answer="Updated answer.",
                category="Return",
            )

        self.assertEqual(document_id, 55)


if __name__ == "__main__":
    unittest.main()
