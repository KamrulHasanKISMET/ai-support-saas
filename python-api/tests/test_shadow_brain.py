"""
Tests for the Phase 1 Shadow (Passive) Language Brain --
app/language/shadow_brain.py (docs/LANGUAGE_INTELLIGENCE.md).

Same style as tests/test_agent_foundation.py: a fake in-memory session
satisfying just the execute()/commit() shape needed, not a real
Postgres connection. embedding_service.embed()/embed_batch() are
mocked (same reasoning tests/test_language_engine.py gives for mocking
the LLM network boundary and nothing else) -- everything else
(seeding logic, the nearest-match query construction, prediction
shaping, error isolation) runs for real.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.language.shadow_brain import SHADOW_BRAIN_VERSION, ShadowBrain
from app.language.shadow_brain_types import BrainPrediction


class FakeRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """Returns pre-programmed results in call order; records executes/commits."""

    def __init__(self, results):
        self._results = list(results)
        self.executed_queries = []
        self.commit_count = 0

    async def execute(self, query, params=None):
        self.executed_queries.append((query, params))
        return self._results.pop(0)

    async def commit(self):
        self.commit_count += 1


class FailingSession(FakeSession):
    async def execute(self, query, params=None):
        self.executed_queries.append((query, params))
        raise RuntimeError("db temporarily unavailable")


def mock_embedding(embed_return=None, embed_batch_return=None):
    patches = []
    if embed_return is not None:
        patches.append(
            patch(
                "app.language.shadow_brain.embedding_service.embed",
                new=AsyncMock(return_value=embed_return),
            )
        )
    if embed_batch_return is not None:
        patches.append(
            patch(
                "app.language.shadow_brain.embedding_service.embed_batch",
                new=AsyncMock(return_value=embed_batch_return),
            )
        )
    return patches


class TestShadowBrainPredict(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_on_empty_message(self):
        brain = ShadowBrain()
        session = FakeSession([])

        result = await brain.predict(session, tenant_id=1, normalized_message="   ")

        self.assertIsNone(result)
        # Nothing was queried at all -- an empty message is a no-op,
        # not something worth a seeding check or an embedding call.
        self.assertEqual(session.executed_queries, [])

    async def test_already_seeded_tenant_skips_seeding_and_returns_match(self):
        brain = ShadowBrain()
        # 1st execute(): COUNT check (tenant already has clusters) --
        # 2nd execute(): the nearest-match query itself.
        session = FakeSession(
            [
                FakeResult([FakeRow(n=5)]),
                FakeResult(
                    [
                        FakeRow(
                            id=42,
                            intent="PRICE_INQUIRY",
                            example_message="দাম কত?",
                            similarity=0.91,
                        )
                    ]
                ),
            ]
        )

        with patch(
            "app.language.shadow_brain.embedding_service.embed",
            new=AsyncMock(return_value=[0.1, 0.2, 0.3]),
        ) as embed_mock:
            result = await brain.predict(session, tenant_id=1, normalized_message="koto taka")

        self.assertIsInstance(result, BrainPrediction)
        self.assertEqual(result.predicted_intent, "PRICE_INQUIRY")
        self.assertEqual(result.cluster_id, 42)
        self.assertAlmostEqual(result.similarity, 0.91)
        self.assertEqual(result.matched_example, "দাম কত?")
        # Only the query message was embedded -- no seeding embedding
        # call happened since the tenant already had clusters.
        embed_mock.assert_awaited_once_with("koto taka")
        self.assertEqual(len(session.executed_queries), 2)
        self.assertEqual(session.commit_count, 0)

    async def test_cold_start_seeds_defaults_then_matches(self):
        brain = ShadowBrain()
        n_examples = sum(1 for _ in _iter_default_examples())

        session = FakeSession(
            [
                FakeResult([FakeRow(n=0)]),  # COUNT: no clusters yet
                *[FakeResult([]) for _ in range(n_examples)],  # N seed inserts
                FakeResult(
                    [
                        FakeRow(
                            id=7,
                            intent="ORDER_STATUS",
                            example_message="where is my order",
                            similarity=0.88,
                        )
                    ]
                ),
            ]
        )

        with patch(
            "app.language.shadow_brain.embedding_service.embed_batch",
            new=AsyncMock(return_value=[[0.0] * 3 for _ in range(n_examples)]),
        ) as embed_batch_mock, patch(
            "app.language.shadow_brain.embedding_service.embed",
            new=AsyncMock(return_value=[0.0] * 3),
        ):
            result = await brain.predict(
                session, tenant_id=2, normalized_message="order kobe pabo"
            )

        embed_batch_mock.assert_awaited_once()
        self.assertEqual(result.predicted_intent, "ORDER_STATUS")
        self.assertEqual(result.cluster_id, 7)
        # 1 COUNT + N inserts + 1 match query
        self.assertEqual(len(session.executed_queries), 1 + n_examples + 1)
        self.assertEqual(session.commit_count, 1)

    async def test_no_clusters_and_no_examples_seeded_returns_none(self):
        brain = ShadowBrain()
        n_examples = sum(1 for _ in _iter_default_examples())
        session = FakeSession(
            [
                FakeResult([FakeRow(n=0)]),
                *[FakeResult([]) for _ in range(n_examples)],
                FakeResult([]),  # match query somehow returns nothing
            ]
        )

        with patch(
            "app.language.shadow_brain.embedding_service.embed_batch",
            new=AsyncMock(return_value=[[0.0] * 3 for _ in range(n_examples)]),
        ), patch(
            "app.language.shadow_brain.embedding_service.embed",
            new=AsyncMock(return_value=[0.0] * 3),
        ):
            result = await brain.predict(session, tenant_id=3, normalized_message="hi")

        self.assertIsNone(result)

    async def test_any_failure_is_isolated_and_returns_none(self):
        brain = ShadowBrain()
        session = FailingSession([])

        try:
            result = await brain.predict(session, tenant_id=1, normalized_message="hello")
        except Exception as exc:
            self.fail(f"predict() raised unexpectedly: {exc}")

        self.assertIsNone(result)

    async def test_embedding_provider_failure_is_isolated_and_returns_none(self):
        brain = ShadowBrain()
        session = FakeSession([FakeResult([FakeRow(n=3)])])

        with patch(
            "app.language.shadow_brain.embedding_service.embed",
            new=AsyncMock(side_effect=RuntimeError("no OPENAI_API_KEY configured")),
        ):
            try:
                result = await brain.predict(session, tenant_id=1, normalized_message="hello")
            except Exception as exc:
                self.fail(f"predict() raised unexpectedly: {exc}")

        self.assertIsNone(result)

    def test_version_string_is_stable(self):
        # Pinned so a change to the matching approach is a deliberate,
        # reviewed bump of this constant, not a silent drift -- every
        # language_experiences.brain_version row depends on this value
        # meaning one specific matching approach.
        self.assertEqual(SHADOW_BRAIN_VERSION, "shadow-v1-nearest-example")


def _iter_default_examples():
    from app.language.shadow_brain import _DEFAULT_CLUSTER_EXAMPLES

    for examples in _DEFAULT_CLUSTER_EXAMPLES.values():
        yield from examples


if __name__ == "__main__":
    unittest.main()
