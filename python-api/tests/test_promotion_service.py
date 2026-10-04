"""
Tests for PromotionService (Phase 3 — docs/LANGUAGE_INTELLIGENCE.md;
generalization gate — Phase 4, docs/GENERAL_LANGUAGE_BRAIN.md §5.5).

Tests cover:
    - no active canary → 'no_canary'
    - below MIN_CANARY_SAMPLES → 'pending'
    - was_correct_rate >= MIN_PROMOTE_ACCURACY → 'promoted'
    - was_correct_rate < MIN_ROLLBACK_ACCURACY → 'rolled_back'
    - between thresholds → 'pending'
    - DB failure → 'error', never raises
    - start_canary: upserts canary_splits, logs canary_start
    - constants are stable (pin them)
    - generalization gate: pass → 'promoted', fail → 'held_for_generalization',
      eval error → 'pending' (retry, no rollback), no eval coverage for
      the intent → 'not_applicable' (treated as pass-through)

Accuracy-only tests below (TestPromotionService) deliberately use
UNCOVERED_INTENT, a made-up intent that is NOT in
app/language/eval_sets/multilingual_intents_v1.json (the eval set now
covers all 10 real IntentType values, so no real intent can be used
here any more) so the generalization gate is naturally "not_applicable" and they keep exercising only the
accuracy path they always tested. TestPromotionServiceGeneralizationGate
below uses intents that ARE covered (DELIVERY_INFO, RETURN_REQUEST) to
exercise the gate itself, with generalization_eval.run_for_tenant
patched so no real embedding/LLM provider is needed.

Run:
    docker compose exec python-api python -m unittest tests.test_promotion_service -v
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.language.cluster_builder_types import PromotionCheckResult
from app.language.generalization_eval import GeneralizationReport, Slice
from app.language.promotion_service import (
    MIN_CANARY_SAMPLES,
    MIN_PROMOTE_ACCURACY,
    MIN_ROLLBACK_ACCURACY,
    PromotionService,
)


# ── Fake session ───────────────────────────────────────────────────────


# An intent with NO held-out eval cases, so the generalization gate is
# "not_applicable". This used to be "NEGOTIATION", but the eval set now
# covers all 10 IntentType values (Phase 5 Track 2), which silently made
# four tests here call the real eval. A made-up name is stable no matter
# how coverage grows; the guard test below fails loudly if someone ever
# adds cases for it.
UNCOVERED_INTENT = "INTENT_WITHOUT_EVAL_CASES"


class TestUncoveredIntentFixture(unittest.TestCase):
    def test_uncovered_intent_really_has_no_eval_cases(self):
        from app.language.generalization_eval import load_eval_set
        self.assertNotIn(UNCOVERED_INTENT, {c.intent for c in load_eval_set()})


class FakeResult:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class CanaryRow:
    def __init__(self, canary_pct=5.0, candidate_cluster_id=99,
                 cand_threshold=0.80, cand_agreement=0.92):
        self.canary_pct = canary_pct
        self.candidate_cluster_id = candidate_cluster_id
        self.cand_threshold = cand_threshold
        self.cand_agreement = cand_agreement


class AccuracyRow:
    def __init__(self, sample_count, was_correct_rate):
        self.sample_count = sample_count
        self.was_correct_rate = was_correct_rate


class FakeSession:
    def __init__(self, canary_row=None, accuracy_row=None):
        self._canary_row = canary_row
        self._accuracy_row = accuracy_row
        self.executed = []
        self.commits = 0

    async def execute(self, query, params=None):
        sql = str(query).upper().strip()
        self.executed.append(sql[:50])
        if "FROM CANARY_SPLITS" in sql and "FROM ROUTING_DECISIONS" not in sql:
            return FakeResult(self._canary_row)
        if "FROM ROUTING_DECISIONS" in sql:
            return FakeResult(self._accuracy_row)
        return FakeResult()

    async def commit(self):
        self.commits += 1


class FailingSession(FakeSession):
    async def execute(self, query, params=None):
        raise RuntimeError("db down")


# ── Tests ──────────────────────────────────────────────────────────────

class TestPromotionService(unittest.IsolatedAsyncioTestCase):

    async def test_no_canary_returns_no_canary(self):
        svc = PromotionService()
        session = FakeSession(canary_row=None)
        result = await svc.check_and_promote(session, tenant_id=1, intent="PRICE_INQUIRY")
        self.assertEqual(result.action, "no_canary")

    async def test_below_min_samples_returns_pending(self):
        svc = PromotionService()
        session = FakeSession(
            canary_row=CanaryRow(),
            accuracy_row=AccuracyRow(sample_count=MIN_CANARY_SAMPLES - 1, was_correct_rate=0.99),
        )
        result = await svc.check_and_promote(session, tenant_id=1, intent="COMPLAINT")
        self.assertEqual(result.action, "pending")
        self.assertEqual(result.cluster_id, 99)

    async def test_high_accuracy_promotes(self):
        svc = PromotionService()
        session = FakeSession(
            canary_row=CanaryRow(),
            accuracy_row=AccuracyRow(
                sample_count=MIN_CANARY_SAMPLES + 10,
                was_correct_rate=MIN_PROMOTE_ACCURACY,
            ),
        )
        # UNCOVERED_INTENT has no generalization eval coverage -> gate is
        # "not_applicable", so this exercises the accuracy path only.
        result = await svc.check_and_promote(session, tenant_id=1, intent=UNCOVERED_INTENT)
        self.assertEqual(result.action, "promoted")
        self.assertAlmostEqual(result.was_correct_rate, MIN_PROMOTE_ACCURACY)
        self.assertGreater(session.commits, 0)

    async def test_above_promote_threshold_promotes(self):
        svc = PromotionService()
        session = FakeSession(
            canary_row=CanaryRow(),
            accuracy_row=AccuracyRow(
                sample_count=MIN_CANARY_SAMPLES,
                was_correct_rate=0.99,
            ),
        )
        # UNCOVERED_INTENT has no generalization eval coverage -> "not_applicable".
        result = await svc.check_and_promote(session, tenant_id=2, intent=UNCOVERED_INTENT)
        self.assertEqual(result.action, "promoted")

    async def test_low_accuracy_rolls_back(self):
        svc = PromotionService()
        session = FakeSession(
            canary_row=CanaryRow(),
            accuracy_row=AccuracyRow(
                sample_count=MIN_CANARY_SAMPLES,
                was_correct_rate=MIN_ROLLBACK_ACCURACY - 0.01,
            ),
        )
        result = await svc.check_and_promote(session, tenant_id=1, intent=UNCOVERED_INTENT)
        self.assertEqual(result.action, "rolled_back")
        self.assertGreater(session.commits, 0)

    async def test_between_thresholds_returns_pending(self):
        svc = PromotionService()
        mid_rate = (MIN_PROMOTE_ACCURACY + MIN_ROLLBACK_ACCURACY) / 2
        session = FakeSession(
            canary_row=CanaryRow(),
            accuracy_row=AccuracyRow(
                sample_count=MIN_CANARY_SAMPLES + 5,
                was_correct_rate=mid_rate,
            ),
        )
        result = await svc.check_and_promote(session, tenant_id=1, intent="PRODUCT_INFO")
        self.assertEqual(result.action, "pending")
        self.assertAlmostEqual(result.was_correct_rate, mid_rate, places=5)

    async def test_db_failure_returns_error_never_raises(self):
        svc = PromotionService()
        session = FailingSession(canary_row=CanaryRow())
        try:
            result = await svc.check_and_promote(session, tenant_id=1, intent="COMPLAINT")
        except Exception as exc:
            self.fail(f"check_and_promote raised unexpectedly: {exc}")
        self.assertEqual(result.action, "error")

    async def test_start_canary_commits(self):
        svc = PromotionService()
        session = FakeSession()
        await svc.start_canary(session, tenant_id=1, intent="PRICE_INQUIRY",
                               candidate_cluster_id=7, canary_pct=5.0)
        self.assertGreater(session.commits, 0)

    async def test_exact_promote_threshold_promotes(self):
        """Edge: exactly at MIN_PROMOTE_ACCURACY should promote."""
        svc = PromotionService()
        session = FakeSession(
            canary_row=CanaryRow(),
            accuracy_row=AccuracyRow(
                sample_count=MIN_CANARY_SAMPLES,
                was_correct_rate=MIN_PROMOTE_ACCURACY,
            ),
        )
        # GENERAL_QUESTION has no generalization eval coverage -> "not_applicable".
        result = await svc.check_and_promote(session, tenant_id=1, intent=UNCOVERED_INTENT)
        self.assertEqual(result.action, "promoted")

    async def test_exact_rollback_threshold_rolls_back(self):
        """Edge: exactly at MIN_ROLLBACK_ACCURACY - 0.001 should roll back."""
        svc = PromotionService()
        session = FakeSession(
            canary_row=CanaryRow(),
            accuracy_row=AccuracyRow(
                sample_count=MIN_CANARY_SAMPLES,
                was_correct_rate=MIN_ROLLBACK_ACCURACY - 0.001,
            ),
        )
        result = await svc.check_and_promote(session, tenant_id=1, intent="COMPLAINT")
        self.assertEqual(result.action, "rolled_back")


class TestPromotionServiceGeneralizationGate(unittest.IsolatedAsyncioTestCase):
    """
    DELIVERY_INFO and RETURN_REQUEST both have held-out cases in
    app/language/eval_sets/multilingual_intents_v1.json, so
    _run_generalization_gate() actually runs generalization_eval's
    passes_gate() for them instead of short-circuiting to
    "not_applicable". generalization_eval.run_for_tenant is patched at
    its promotion_service import site so no real embedding/LLM
    provider or cluster data is needed.
    """

    def _passing_session(self):
        return FakeSession(
            canary_row=CanaryRow(),
            accuracy_row=AccuracyRow(
                sample_count=MIN_CANARY_SAMPLES + 5,
                was_correct_rate=MIN_PROMOTE_ACCURACY,
            ),
        )

    async def test_gate_pass_promotes(self):
        svc = PromotionService()
        session = self._passing_session()
        report = GeneralizationReport(
            overall=Slice(n=20, correct=19),
            by_language={"en": Slice(n=10, correct=10), "bn": Slice(n=10, correct=9)},
            concept_consistency=0.9,
        )
        with patch(
            "app.language.promotion_service.run_generalization_eval",
            new=AsyncMock(return_value=report),
        ):
            result = await svc.check_and_promote(session, tenant_id=1, intent="DELIVERY_INFO")
        self.assertEqual(result.action, "promoted")
        self.assertGreater(session.commits, 0)

    async def test_gate_fail_holds_for_generalization(self):
        svc = PromotionService()
        session = self._passing_session()
        commits_before = session.commits
        # Overall accuracy far below the 0.85 default gate floor.
        report = GeneralizationReport(overall=Slice(n=20, correct=10))
        with patch(
            "app.language.promotion_service.run_generalization_eval",
            new=AsyncMock(return_value=report),
        ):
            result = await svc.check_and_promote(session, tenant_id=1, intent="RETURN_REQUEST")
        self.assertEqual(result.action, "held_for_generalization")
        self.assertIsNotNone(result.notes)
        self.assertGreater(result.cluster_id, 0)
        # Candidate was retired and canary cleared, same DB effect as a rollback.
        self.assertGreater(session.commits, commits_before)

    async def test_gate_error_holds_pending_without_rollback(self):
        svc = PromotionService()
        session = self._passing_session()
        commits_before = session.commits
        with patch(
            "app.language.promotion_service.run_generalization_eval",
            new=AsyncMock(side_effect=RuntimeError("embedding provider down")),
        ):
            result = await svc.check_and_promote(session, tenant_id=1, intent="DELIVERY_INFO")
        self.assertEqual(result.action, "pending")
        # An eval failure must not retire the candidate or clear the canary --
        # only the log-independent accuracy commits from earlier should exist.
        self.assertEqual(session.commits, commits_before)

    async def test_no_coverage_intent_is_not_applicable_and_promotes(self):
        svc = PromotionService()
        session = self._passing_session()
        with patch(
            "app.language.promotion_service.run_generalization_eval",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            result = await svc.check_and_promote(session, tenant_id=1, intent=UNCOVERED_INTENT)
        self.assertEqual(result.action, "promoted")


class TestPromotionConstants(unittest.TestCase):
    def test_min_promote_accuracy(self):
        self.assertAlmostEqual(MIN_PROMOTE_ACCURACY, 0.95, places=5)

    def test_min_rollback_accuracy(self):
        self.assertAlmostEqual(MIN_ROLLBACK_ACCURACY, 0.85, places=5)

    def test_min_canary_samples(self):
        self.assertEqual(MIN_CANARY_SAMPLES, 20)

    def test_promote_above_rollback(self):
        """Promote threshold must always be strictly above rollback threshold."""
        self.assertGreater(MIN_PROMOTE_ACCURACY, MIN_ROLLBACK_ACCURACY)


if __name__ == "__main__":
    unittest.main()
