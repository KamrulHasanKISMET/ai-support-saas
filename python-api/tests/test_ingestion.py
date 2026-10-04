"""Tests for app/rag/ingestion.py (P6R-1 knowledge ingestion). Fakes only.
    docker compose exec python-api python -m unittest tests.test_ingestion -v
"""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import _sa_stub  # noqa: F401
from app.rag import ingestion as ing

ROOT = Path(__file__).resolve().parents[2]
DIM = ing.EMBEDDING_DIM


class TestClean(unittest.TestCase):
    def test_normalises_whitespace_and_newlines(self):
        self.assertEqual(ing.clean_text("a  \t b\r\n\r\n\r\n\r\nc  "), "a b\n\nc")

    def test_drops_control_and_zero_width_but_keeps_zwnj_zwj(self):
        raw = "a\x00b\u200bc\u09b0\u200d\u09cd\u09af d\u200ce"
        out = ing.clean_text(raw)
        self.assertNotIn("\x00", out)
        self.assertNotIn("\u200b", out)
        self.assertIn("\u200d", out)     # ZWJ kept (Bengali shaping)
        self.assertIn("\u200c", out)     # ZWNJ kept

    def test_empty_and_none(self):
        self.assertEqual(ing.clean_text(""), "")
        self.assertEqual(ing.clean_text(None), "")

    def test_hash_is_stable_and_content_sensitive(self):
        self.assertEqual(ing.content_hash("x"), ing.content_hash("x"))
        self.assertNotEqual(ing.content_hash("x"), ing.content_hash("y"))
        self.assertEqual(len(ing.content_hash("x")), 64)


def words(s): return set(s.split())


class TestChunk(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(ing.chunk_text("Delivery takes 3 days inside Dhaka."), ["Delivery takes 3 days inside Dhaka."])

    def test_no_chunk_exceeds_limit_and_no_words_lost(self):
        text = " ".join(f"Sentence number {i} is about policy {i}." for i in range(300))
        chunks = ing.chunk_text(text, max_chars=300, overlap=60)
        self.assertGreater(len(chunks), 5)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertTrue(words(text) <= words(" ".join(chunks)))

    def test_bengali_danda_splits_sentences(self):
        text = "\u09a1\u09c7\u09b2\u09bf\u09ad\u09be\u09b0\u09bf \u09e9 \u09a6\u09bf\u09a8 \u09b2\u09be\u0997\u09c7\u0964 " * 60
        chunks = ing.chunk_text(text.strip(), max_chars=200, overlap=40)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertTrue(all(c.rstrip().endswith("\u0964") for c in chunks))   # never cut mid-sentence

    def test_overlap_repeats_a_trailing_sentence(self):
        text = " ".join(f"Fact {i:03d} here." for i in range(80))
        chunks = ing.chunk_text(text, max_chars=200, overlap=40)
        for a, b in zip(chunks, chunks[1:]):
            last = a.split(". ")[-1] if ". " in a else a
            self.assertIn(last.rstrip(".").strip()[-8:], b)

    def test_giant_word_is_hard_split(self):
        chunks = ing.chunk_text("x" * 5000, max_chars=300, overlap=50)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertEqual("".join(chunks).count("x"), 5000)

    def test_deterministic(self):
        t = "A b c. " * 400
        self.assertEqual(ing.chunk_text(t), ing.chunk_text(t))

    def test_tiny_chunks_are_merged_not_dropped(self):
        chunks = ing.chunk_text("Long enough first sentence here for a chunk.\n\nOk.", max_chars=200, overlap=20)
        self.assertIn("Ok.", " ".join(chunks))

    def test_bad_parameters_refused(self):
        for mc, ov in ((10, 0), (200, 150), (200, -1)):
            with self.assertRaises(ing.IngestionError):
                ing.chunk_text("hello world " * 20, mc, ov)


class FakeDB:
    def __init__(self, dup=None, old=None, fail_on=None):
        self.dup, self.old, self.fail_on = dup, old, fail_on
        self.log, self.commits, self.rollbacks = [], 0, 0
        self.next_id = 7

    async def execute(self, q, params=None):
        s = str(q)
        self.log.append((s, params))
        if self.fail_on and self.fail_on in s:
            raise RuntimeError("db down")
        if "content_sha256 = :h" in s:
            return SimpleNamespace(first=lambda: self.dup)
        if "source = :s" in s:
            return SimpleNamespace(first=lambda: self.old)
        if "RETURNING id" in s:
            return SimpleNamespace(first=lambda: SimpleNamespace(id=self.next_id))
        if s.strip().startswith("DELETE"):
            return SimpleNamespace(rowcount=1 if (params or {}).get("i") == 7 and params.get("t") == 1 else 0)
        return iter([])

    async def commit(self): self.commits += 1
    async def rollback(self): self.rollbacks += 1

    def stmts(self, kw): return [(s, p) for s, p in self.log if kw in s]


async def embed(texts):
    return [[0.1] * DIM for _ in texts]


def run(db, **kw):
    a = dict(tenant_id=1, title="Policy", content="Returns accepted within 7 days. Refunds take 5 days.",
             embed_fn=embed, embedding_model="emb")
    a.update(kw)
    return asyncio.run(ing.ingest_document(db, **a))


class TestIngest(unittest.TestCase):
    def test_creates_document_and_chunks_in_one_commit(self):
        db = FakeDB()
        r = run(db, source="faq.txt", language="en", category="policy")
        self.assertEqual((r.status, r.document_id, r.chunk_count), ("created", 7, 1))
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.stmts("INSERT INTO knowledge_chunks")), 1)
        for _, p in db.stmts("INSERT INTO"):
            self.assertEqual(p["t"], 1)                        # tenant on every insert

    def test_duplicate_content_writes_nothing_and_does_not_embed(self):
        db = FakeDB(dup=SimpleNamespace(id=3, chunk_count=2))
        called = []
        async def emb(t): called.append(1); return await embed(t)
        r = run(db, embed_fn=emb)
        self.assertEqual((r.status, r.document_id, r.chunk_count), ("duplicate", 3, 2))
        self.assertEqual((db.commits, called, db.stmts("INSERT")), (0, [], []))

    def test_same_source_new_text_replaces_in_same_transaction(self):
        db = FakeDB(old=SimpleNamespace(id=5))
        r = run(db, source="faq.txt")
        self.assertEqual(r.status, "replaced")
        dels = db.stmts("DELETE FROM knowledge_documents")
        self.assertEqual(dels[0][1], {"i": 5, "t": 1})         # tenant-scoped delete
        self.assertEqual(db.commits, 1)

    def test_embedding_failure_writes_nothing(self):
        db = FakeDB()
        async def boom(t): raise RuntimeError("provider down")
        with self.assertRaises(RuntimeError):
            run(db, embed_fn=boom)
        self.assertEqual((db.stmts("INSERT"), db.stmts("DELETE"), db.commits), ([], [], 0))

    def test_wrong_dimension_refused_before_any_write(self):
        db = FakeDB()
        async def bad(t): return [[0.1] * 10 for _ in t]
        with self.assertRaises(ing.IngestionError):
            run(db, embed_fn=bad)
        self.assertEqual(db.stmts("INSERT"), [])

    def test_wrong_vector_count_refused(self):
        async def few(t): return []
        with self.assertRaises(ing.IngestionError):
            run(FakeDB(), embed_fn=few)

    def test_db_failure_mid_insert_rolls_back_and_raises(self):
        db = FakeDB(fail_on="INSERT INTO knowledge_chunks")
        with self.assertRaises(RuntimeError):
            run(db)
        self.assertEqual((db.rollbacks, db.commits), (1, 0))

    def test_input_validation(self):
        db = FakeDB()
        for kw in (dict(tenant_id=0), dict(tenant_id="1"), dict(title="  "), dict(title="x" * 256),
                   dict(content=" \n "), dict(source="s" * 256),
                   dict(content="a. " * (ing.MAX_DOC_CHARS // 3 + 10))):
            with self.assertRaises(ing.IngestionError, msg=str(kw)[:40]):
                run(db, **kw)
        self.assertEqual(db.log, [])                            # nothing touched the DB

    def test_too_many_chunks_refused_not_truncated(self):
        big = "Word " * 40000                                   # 200k chars, many chunks
        with self.assertRaises(ing.IngestionError):
            run(FakeDB(), content=big.replace("Word", "Sentence one here."))

    def test_long_documents_are_embedded_in_batches(self):
        sizes = []
        async def emb(t): sizes.append(len(t)); return await embed(t)
        text = " ".join(f"Rule number {i} applies to all orders." for i in range(2000))
        run(FakeDB(), content=text, embed_fn=emb)
        self.assertGreater(len(sizes), 1)
        self.assertTrue(all(s <= ing.EMBED_BATCH for s in sizes))


class TestListDelete(unittest.TestCase):
    def test_list_is_tenant_scoped_and_has_no_content(self):
        db = FakeDB()
        async def go():
            await ing.list_documents(db, 4, limit=9999)
        asyncio.run(go())
        s, p = db.log[0]
        self.assertIn("tenant_id = :t", s)
        self.assertNotIn("raw_content", s)
        self.assertEqual(p, {"t": 4, "n": 500})

    def test_delete_only_own_tenant(self):
        db = FakeDB()
        mine = asyncio.run(ing.delete_document(db, 1, 7))
        other = asyncio.run(ing.delete_document(db, 2, 7))
        self.assertEqual((mine, other), (True, False))


class TestFilesAgree(unittest.TestCase):
    def test_migration_has_columns_and_tenant_scoped_uniques(self):
        sql = (ROOT / "db/init/025_knowledge_ingestion.sql").read_text()
        for c in ("content_sha256", "chunk_count", "embedding_model"):
            self.assertIn(c, sql)
        self.assertIn("(tenant_id, content_sha256)", sql)
        self.assertIn("(tenant_id, source)", sql)
        self.assertIn("025", (ROOT / "scripts/migrate.sh").read_text())

    def test_insert_columns_exist_in_schema(self):
        base = (ROOT / "db/init/003_knowledge_rag.sql").read_text()
        mig = (ROOT / "db/init/025_knowledge_ingestion.sql").read_text()
        for col in ("tenant_id", "title", "source", "language", "category", "raw_content",
                    "content_sha256", "chunk_count", "embedding_model",
                    "document_id", "chunk_index", "content", "embedding"):
            self.assertTrue(col in base or col in mig, col)

    def test_dimension_matches_schema(self):
        self.assertIn(f"vector({DIM})", (ROOT / "db/init/003_knowledge_rag.sql").read_text())

    def test_route_is_secret_gated_registered_and_tenant_required(self):
        r = (ROOT / "python-api/app/api/routes/knowledge.py").read_text()
        self.assertIn("require_internal_secret", r)
        self.assertEqual(r.count('alias="tenantId"'), 3)        # upload, list, delete
        self.assertIn('extra="forbid"', r)
        main = (ROOT / "python-api/app/main.py").read_text()
        self.assertIn("knowledge.router", main)

    def test_kernel_and_context_engine_unchanged_by_ingestion(self):
        for rel in ("kernel/kernel.py", "context/context_engine.py"):
            self.assertNotIn("ingestion", (ROOT / "python-api/app" / rel).read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
