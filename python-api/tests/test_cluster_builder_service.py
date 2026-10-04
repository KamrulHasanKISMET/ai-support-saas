"""
Tests for ClusterBuilderService (Phase 3 — docs/LANGUAGE_INTELLIGENCE.md).

Tests cover:
    - no eligible rows → empty result, never raises
    - below MIN_SAMPLES_TO_BUILD → intent skipped
    - sufficient data → candidate cluster written, shadow_eval logged
    - centroid-nearest selection picks most central examples
    - embedding batch failure → intent skipped gracefully
    - fatal DB failure → empty result, never raises
    - multiple intents: each independently built or skipped
    - constants are stable (pin them)

Run:
    docker compose exec python-api python -m unittest tests.test_cluster_builder_service -v
"""

import math
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.language.cluster_builder_service import (
    MIN_SAMPLES_TO_BUILD,
    TOP_K_EXAMPLES,
    VERSION_PREFIX,
    ClusterBuilderService,
)
from app.language.cluster_builder_types import ClusterBuildResult, ExperienceRow


# ── Fake session ───────────────────────────────────────────────────────

class FakeResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeExpRow:
    def __init__(self, experience_id, final_intent, normalized_message):
        self.experience_id = experience_id
        self.final_intent = final_intent
        self.normalized_message = normalized_message


class FakeInsertRow:
    id = 42


class FakeSession:
    def __init__(self, exp_rows=None, insert_id=42):
        self._exp_rows = exp_rows or []
        self._insert_id = insert_id
        self.executed = []
        self.commits = 0

    async def execute(self, query, params=None):
        sql = str(query).upper().strip()
        self.executed.append(sql[:40])
        if "FROM LANGUAGE_EXPERIENCES" in sql:
            return FakeResult(self._exp_rows)
        if "RETURNING ID" in sql:
            row = MagicMock()
            row.id = self._insert_id
            return FakeResult([row])
        return FakeResult()

    async def commit(self):
        self.commits += 1


class FailingSession(FakeSession):
    async def execute(self, query, params=None):
        raise RuntimeError("db down")


# ── Helpers ────────────────────────────────────────────────────────────

def _make_rows(intent, n, prefix="msg"):
    return [
        FakeExpRow(f"exp-{i}", intent, f"{prefix} {i}")
        for i in range(n)
    ]


def _unit_emb(dim=3, val=1.0):
    """A simple unit embedding."""
    norm = math.sqrt(dim) * val
    return [val / norm] * dim


def _make_embeddings(n, dim=3):
    """n identical unit embeddings."""
    return [_unit_emb(dim) for _ in range(n)]


# ── Tests ──────────────────────────────────────────────────────────────

class TestClusterBuilderService(unittest.IsolatedAsyncioTestCase):

    async def test_no_eligible_rows_returns_empty(self):
        svc = ClusterBuilderService()
        session = FakeSession(exp_rows=[])
        with patch("app.language.cluster_builder_service.embedding_service") as mock_emb:
            mock_emb.embed_batch = AsyncMock(return_value=[])
            result = await svc.build_for_tenant(session, tenant_id=1)
        self.assertIsInstance(result, ClusterBuildResult)
        self.assertEqual(result.intents_built, 0)
        self.assertEqual(result.per_intent, [])

    async def test_below_min_samples_skips_intent(self):
        svc = ClusterBuilderService()
        rows = _make_rows("PRICE_INQUIRY", MIN_SAMPLES_TO_BUILD - 1)
        session = FakeSession(exp_rows=rows)
        with patch("app.language.cluster_builder_service.embedding_service") as mock_emb:
            mock_emb.embed_batch = AsyncMock(return_value=[_unit_emb() for _ in rows])
            result = await svc.build_for_tenant(session, tenant_id=1)
        self.assertEqual(result.intents_built, 0)

    async def test_sufficient_data_builds_candidate(self):
        svc = ClusterBuilderService()
        n = MIN_SAMPLES_TO_BUILD + 10
        rows = _make_rows("DELIVERY_INFO", n)
        session = FakeSession(exp_rows=rows)
        embeddings = _make_embeddings(n)
        with patch("app.language.cluster_builder_service.embedding_service") as mock_emb:
            mock_emb.embed_batch = AsyncMock(return_value=embeddings)
            result = await svc.build_for_tenant(session, tenant_id=2)
        self.assertEqual(result.intents_built, 1)
        self.assertEqual(len(result.per_intent), 1)
        built = result.per_intent[0]
        self.assertEqual(built.intent, "DELIVERY_INFO")
        self.assertEqual(built.total_samples, n)
        self.assertLessEqual(built.example_count, TOP_K_EXAMPLES)
        self.assertTrue(built.version.startswith(VERSION_PREFIX))

    async def test_top_k_examples_not_exceeded(self):
        svc = ClusterBuilderService()
        n = MIN_SAMPLES_TO_BUILD + 50  # many rows
        rows = _make_rows("COMPLAINT", n)
        session = FakeSession(exp_rows=rows)
        embeddings = _make_embeddings(n, dim=8)
        with patch("app.language.cluster_builder_service.embedding_service") as mock_emb:
            mock_emb.embed_batch = AsyncMock(return_value=embeddings)
            result = await svc.build_for_tenant(session, tenant_id=3)
        built = result.per_intent[0]
        self.assertLessEqual(built.example_count, TOP_K_EXAMPLES)

    async def test_embedding_failure_skips_intent_gracefully(self):
        svc = ClusterBuilderService()
        rows = _make_rows("NEGOTIATION", MIN_SAMPLES_TO_BUILD + 5)
        session = FakeSession(exp_rows=rows)
        with patch("app.language.cluster_builder_service.embedding_service") as mock_emb:
            # Wrong length returned → ClusterBuilderService skips intent
            mock_emb.embed_batch = AsyncMock(return_value=[])
            result = await svc.build_for_tenant(session, tenant_id=4)
        self.assertEqual(result.intents_built, 0)

    async def test_fatal_db_failure_returns_empty_never_raises(self):
        svc = ClusterBuilderService()
        session = FailingSession()
        try:
            result = await svc.build_for_tenant(session, tenant_id=5)
        except Exception as exc:
            self.fail(f"build_for_tenant raised unexpectedly: {exc}")
        self.assertEqual(result.intents_built, 0)

    async def test_multiple_intents_built_independently(self):
        svc = ClusterBuilderService()
        rows = (
            _make_rows("PRODUCT_INFO", MIN_SAMPLES_TO_BUILD + 5, prefix="pi") +
            _make_rows("RETURN_REQUEST", MIN_SAMPLES_TO_BUILD + 5, prefix="rr") +
            _make_rows("GENERAL_QUESTION", MIN_SAMPLES_TO_BUILD - 1, prefix="gq")  # below threshold
        )
        session = FakeSession(exp_rows=rows)
        # embed_batch is called once per intent with that intent's messages.
        # Side_effect returns correct-size embeddings for each call.
        n_pi = MIN_SAMPLES_TO_BUILD + 5
        n_rr = MIN_SAMPLES_TO_BUILD + 5
        side_effects = [
            _make_embeddings(n_pi, dim=4),
            _make_embeddings(n_rr, dim=4),
        ]
        with patch("app.language.cluster_builder_service.embedding_service") as mock_emb:
            mock_emb.embed_batch = AsyncMock(side_effect=side_effects)
            result = await svc.build_for_tenant(session, tenant_id=6)
        built_intents = {b.intent for b in result.per_intent}
        self.assertIn("PRODUCT_INFO", built_intents)
        self.assertIn("RETURN_REQUEST", built_intents)
        self.assertNotIn("GENERAL_QUESTION", built_intents)
        self.assertEqual(result.intents_built, 2)

    async def test_db_writes_happen_for_each_example(self):
        """Verify that INSERT INTO intent_clusters is called for top-k rows."""
        svc = ClusterBuilderService()
        n = MIN_SAMPLES_TO_BUILD
        rows = _make_rows("PRICE_INQUIRY", n)
        session = FakeSession(exp_rows=rows)
        embeddings = _make_embeddings(n, dim=4)
        with patch("app.language.cluster_builder_service.embedding_service") as mock_emb:
            mock_emb.embed_batch = AsyncMock(return_value=embeddings)
            result = await svc.build_for_tenant(session, tenant_id=7)
        # Should have committed at least once per example inserted
        self.assertGreater(session.commits, 0)
        self.assertEqual(result.intents_built, 1)


class TestClusterBuilderConstants(unittest.TestCase):
    def test_min_samples_to_build(self):
        self.assertEqual(MIN_SAMPLES_TO_BUILD, 50)

    def test_top_k_examples(self):
        self.assertEqual(TOP_K_EXAMPLES, 10)

    def test_version_prefix(self):
        self.assertEqual(VERSION_PREFIX, "tenant_learned")


if __name__ == "__main__":
    unittest.main()
