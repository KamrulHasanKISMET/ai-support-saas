"""
Tests for CalibrationService (Phase 2 — docs/LANGUAGE_INTELLIGENCE.md).

Same style as the existing test suite: fake in-memory sessions, no real
Postgres. Tests cover:
    - no shadow data → empty result, nothing written
    - below MIN_SAMPLE_COUNT → threshold stays None
    - agreement_rate below MIN_AGREEMENT_RATE at all thresholds → None
    - lowest qualifying threshold is chosen (most coverage)
    - threshold found → intent_clusters updated
    - per-intent failures are isolated (other intents still calibrated)
    - calibration_runs row is always attempted
    - fatal DB failure returns empty CalibrationResult, never raises

Run:
    docker compose exec python-api python -m unittest tests.test_calibration_service -v
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.language.calibration_service import (
    MIN_AGREEMENT_RATE,
    MIN_SAMPLE_COUNT,
    THRESHOLD_SCAN_MAX,
    THRESHOLD_SCAN_MIN,
    THRESHOLD_SCAN_STEP,
    CalibrationService,
)
from app.language.calibration_types import CalibrationResult, IntentCalibrationStats


# ── Helpers ────────────────────────────────────────────────────────────

def _make_shadow_row(intent: str, similarity: float, agreement: bool) -> dict:
    return {"final_intent": intent, "similarity": similarity, "agreement": agreement}


def _make_rows(intent: str, n_agree: int, n_disagree: int, similarity: float = 0.85) -> list[dict]:
    """Generate agreed + disagreed rows all at the given similarity."""
    rows = [_make_shadow_row(intent, similarity, True) for _ in range(n_agree)]
    rows += [_make_shadow_row(intent, similarity, False) for _ in range(n_disagree)]
    return rows


# ── _compute_stats_for_intent (pure, no DB) ────────────────────────────

class TestComputeStatsForIntent(unittest.TestCase):
    def setUp(self):
        self.svc = CalibrationService()

    def test_below_min_sample_count_returns_none_threshold(self):
        # 10 perfectly-agreeing rows, but MIN_SAMPLE_COUNT=30
        rows = _make_rows("PRICE_INQUIRY", n_agree=10, n_disagree=0, similarity=0.90)
        stats = self.svc._compute_stats_for_intent("PRICE_INQUIRY", rows)
        self.assertIsNone(stats.similarity_threshold)
        self.assertIsNone(stats.agreement_rate)
        self.assertEqual(stats.sample_count, 10)

    def test_agreement_rate_below_min_at_all_thresholds_returns_none(self):
        # 40 rows, only 50% agreement at every similarity -- never hits 90%
        rows = _make_rows("COMPLAINT", n_agree=20, n_disagree=20, similarity=0.80)
        stats = self.svc._compute_stats_for_intent("COMPLAINT", rows)
        self.assertIsNone(stats.similarity_threshold)
        self.assertIsNone(stats.agreement_rate)
        self.assertEqual(stats.sample_count, 40)

    def test_finds_lowest_qualifying_threshold(self):
        # 50 rows at similarity=0.60 with 95% agreement → threshold should be 0.50
        # (the lowest scan start, since all rows are at 0.60 >= every threshold we try
        # from 0.50 upward and all agree at 95%)
        rows = _make_rows("PRODUCT_INFO", n_agree=47, n_disagree=3, similarity=0.60)
        stats = self.svc._compute_stats_for_intent("PRODUCT_INFO", rows)
        # threshold=0.50 → all 50 rows pass (similarity=0.60 >= 0.50),
        # 47/50 = 94% >= 90% -- should be found.
        self.assertIsNotNone(stats.similarity_threshold)
        self.assertAlmostEqual(stats.similarity_threshold, THRESHOLD_SCAN_MIN, places=5)
        self.assertGreaterEqual(stats.agreement_rate, MIN_AGREEMENT_RATE)
        self.assertEqual(stats.sample_count, 50)

    def test_threshold_scan_picks_lower_threshold_over_higher(self):
        """
        Mix: 40 high-similarity rows (sim=0.85, agree=100%) and 40 low-similarity
        rows (sim=0.55, agree=0%).

        Threshold scan trace:
          0.50: all 80 pass (0.55>=0.50, 0.85>=0.50) → 40/80=50% → fails
          0.55: all 80 pass (0.55>=0.55, 0.85>=0.55) → 40/80=50% → fails
          0.60: only 40 high rows pass (0.85>=0.60, 0.55<0.60) → 40/40=100% → QUALIFIES

        Lowest qualifying threshold = 0.60 (not 0.80).
        The "lowest = most coverage" rule means 0.60 is the right answer.
        """
        high = [_make_shadow_row("DELIVERY_INFO", 0.85, True) for _ in range(40)]
        low  = [_make_shadow_row("DELIVERY_INFO", 0.55, False) for _ in range(40)]
        rows = high + low
        stats = self.svc._compute_stats_for_intent("DELIVERY_INFO", rows)
        self.assertIsNotNone(stats.similarity_threshold)
        self.assertAlmostEqual(stats.similarity_threshold, 0.60, places=5)
        self.assertAlmostEqual(stats.agreement_rate, 1.0, places=5)
        self.assertEqual(stats.sample_count, 80)

    def test_exact_min_agreement_rate_qualifies(self):
        # Exactly 90% agreement on MIN_SAMPLE_COUNT rows → qualifies.
        n = MIN_SAMPLE_COUNT
        agree = int(n * MIN_AGREEMENT_RATE)
        disagree = n - agree
        rows = _make_rows("RETURN_REQUEST", n_agree=agree, n_disagree=disagree, similarity=0.75)
        stats = self.svc._compute_stats_for_intent("RETURN_REQUEST", rows)
        self.assertIsNotNone(stats.similarity_threshold)
        self.assertGreaterEqual(stats.agreement_rate, MIN_AGREEMENT_RATE)

    def test_sample_count_is_always_total_regardless_of_threshold(self):
        # 60 rows: 50 at high similarity (all agree), 10 at low (all disagree)
        high = [_make_shadow_row("NEGOTIATION", 0.90, True) for _ in range(50)]
        low  = [_make_shadow_row("NEGOTIATION", 0.50, False) for _ in range(10)]
        rows = high + low
        stats = self.svc._compute_stats_for_intent("NEGOTIATION", rows)
        # sample_count is always total, not just above-threshold rows
        self.assertEqual(stats.sample_count, 60)


# ── run_for_tenant (with fake DB) ─────────────────────────────────────

class FakeResult:
    def __init__(self, rows, rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    def __init__(self, fetch_rows=None, update_rowcount=1):
        # fetch_rows: rows returned by the SELECT on language_experiences
        self._fetch_rows = fetch_rows or []
        self._update_rowcount = update_rowcount
        self.executed_queries = []
        self.commit_count = 0

    async def execute(self, query, params=None):
        self.executed_queries.append((str(query), params))
        sql = str(query).strip().upper()
        if sql.startswith("SELECT") and "LANGUAGE_EXPERIENCES" in sql:
            return FakeResult(self._fetch_rows)
        if sql.startswith("UPDATE"):
            return FakeResult([], rowcount=self._update_rowcount)
        if sql.startswith("INSERT"):
            return FakeResult([])
        return FakeResult([])

    async def commit(self):
        self.commit_count += 1


class FailingSession(FakeSession):
    async def execute(self, query, params=None):
        raise RuntimeError("db temporarily unavailable")


class FakeBrainRow:
    """Simulates a row from language_experiences as returned by asyncpg."""
    def __init__(self, final_intent, brain_prediction_dict):
        import json as _json
        self.final_intent = final_intent
        # Return as dict (asyncpg JSONB) -- _fetch_shadow_rows handles both
        self.brain_prediction = brain_prediction_dict


class TestRunForTenant(unittest.IsolatedAsyncioTestCase):
    async def test_no_shadow_data_returns_empty_result(self):
        svc = CalibrationService()
        session = FakeSession(fetch_rows=[])
        result = await svc.run_for_tenant(session, tenant_id=1)
        self.assertIsInstance(result, CalibrationResult)
        self.assertEqual(result.intents_updated, 0)
        self.assertEqual(result.total_samples, 0)
        self.assertEqual(result.per_intent, [])

    async def test_sufficient_data_updates_clusters_and_records_run(self):
        svc = CalibrationService()
        # 35 agreeing rows for PRICE_INQUIRY at similarity 0.80
        fake_rows = [
            FakeBrainRow(
                "PRICE_INQUIRY",
                {"similarity": 0.80, "agreement": True, "predicted_intent": "PRICE_INQUIRY"},
            )
            for _ in range(35)
        ]
        session = FakeSession(fetch_rows=fake_rows, update_rowcount=2)
        result = await svc.run_for_tenant(session, tenant_id=1)

        self.assertEqual(result.intents_updated, 1)
        self.assertEqual(result.total_samples, 35)
        self.assertEqual(len(result.per_intent), 1)
        stats = result.per_intent[0]
        self.assertEqual(stats.intent, "PRICE_INQUIRY")
        self.assertIsNotNone(stats.similarity_threshold)
        self.assertGreaterEqual(stats.agreement_rate, MIN_AGREEMENT_RATE)
        # Should have committed at least: the UPDATE + the calibration_run INSERT
        self.assertGreaterEqual(session.commit_count, 2)

    async def test_below_sample_count_writes_none_threshold(self):
        svc = CalibrationService()
        # Only 5 rows -- below MIN_SAMPLE_COUNT
        fake_rows = [
            FakeBrainRow(
                "COMPLAINT",
                {"similarity": 0.95, "agreement": True, "predicted_intent": "COMPLAINT"},
            )
            for _ in range(5)
        ]
        session = FakeSession(fetch_rows=fake_rows, update_rowcount=1)
        result = await svc.run_for_tenant(session, tenant_id=2)
        self.assertEqual(len(result.per_intent), 1)
        stats = result.per_intent[0]
        self.assertIsNone(stats.similarity_threshold)
        self.assertIsNone(stats.agreement_rate)
        self.assertEqual(stats.sample_count, 5)
        # UPDATE still fires (to reset calibrated_threshold to NULL)
        update_calls = [q for q, _ in session.executed_queries if "UPDATE" in q.upper()]
        self.assertTrue(len(update_calls) >= 1)

    async def test_fatal_db_failure_returns_empty_result_never_raises(self):
        svc = CalibrationService()
        session = FailingSession()
        try:
            result = await svc.run_for_tenant(session, tenant_id=3)
        except Exception as exc:
            self.fail(f"run_for_tenant() raised unexpectedly: {exc}")
        self.assertIsInstance(result, CalibrationResult)
        self.assertEqual(result.intents_updated, 0)
        self.assertEqual(result.total_samples, 0)

    async def test_multiple_intents_all_calibrated(self):
        svc = CalibrationService()
        # 35 agreeing rows for two intents
        fake_rows = []
        for intent in ["PRODUCT_INFO", "DELIVERY_INFO"]:
            fake_rows += [
                FakeBrainRow(
                    intent,
                    {"similarity": 0.82, "agreement": True, "predicted_intent": intent},
                )
                for _ in range(35)
            ]
        session = FakeSession(fetch_rows=fake_rows, update_rowcount=1)
        result = await svc.run_for_tenant(session, tenant_id=4)
        self.assertEqual(result.total_samples, 70)
        intents_in_result = {s.intent for s in result.per_intent}
        self.assertIn("PRODUCT_INFO", intents_in_result)
        self.assertIn("DELIVERY_INFO", intents_in_result)

    async def test_json_string_brain_prediction_is_parsed(self):
        """
        Some DB drivers return JSONB columns as strings rather than dicts.
        _fetch_shadow_rows must handle both.
        """
        import json as _json
        svc = CalibrationService()

        class StringJsonRow:
            def __init__(self, intent, bp_dict):
                self.final_intent = intent
                self.brain_prediction = _json.dumps(bp_dict)  # string, not dict

        fake_rows = [
            StringJsonRow(
                "RETURN_REQUEST",
                {"similarity": 0.88, "agreement": True, "predicted_intent": "RETURN_REQUEST"},
            )
            for _ in range(35)
        ]
        session = FakeSession(fetch_rows=fake_rows, update_rowcount=1)
        result = await svc.run_for_tenant(session, tenant_id=5)
        self.assertEqual(result.total_samples, 35)
        self.assertEqual(len(result.per_intent), 1)

    async def test_rows_without_similarity_or_agreement_are_skipped(self):
        """Malformed brain_prediction rows are filtered out in _fetch_shadow_rows."""
        svc = CalibrationService()
        bad_rows = [
            FakeBrainRow("GENERAL_QUESTION", {"predicted_intent": "GENERAL_QUESTION"}),  # missing similarity+agreement
            FakeBrainRow("GENERAL_QUESTION", None),  # None brain_prediction
        ]
        session = FakeSession(fetch_rows=bad_rows)
        result = await svc.run_for_tenant(session, tenant_id=6)
        self.assertEqual(result.total_samples, 0)
        self.assertEqual(result.per_intent, [])


# ── Constants are stable (pin them so a change is deliberate) ──────────

class TestCalibrationConstants(unittest.TestCase):
    def test_min_sample_count(self):
        self.assertEqual(MIN_SAMPLE_COUNT, 30)

    def test_min_agreement_rate(self):
        self.assertAlmostEqual(MIN_AGREEMENT_RATE, 0.90, places=5)

    def test_threshold_scan_range(self):
        self.assertAlmostEqual(THRESHOLD_SCAN_MIN, 0.50, places=5)
        self.assertAlmostEqual(THRESHOLD_SCAN_MAX, 0.95, places=5)
        self.assertAlmostEqual(THRESHOLD_SCAN_STEP, 0.05, places=5)


if __name__ == "__main__":
    unittest.main()
