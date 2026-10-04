"""
Tests for RoutingService (Phase 2 — docs/LANGUAGE_INTELLIGENCE.md).

Same style as the existing test suite: fake in-memory sessions, no real
Postgres. Tests cover:
    - no brain prediction → 'shadow' route
    - business-risk intents → routed like any other (calibration-gated);
      autonomous ACTION is gated separately in control_plane
    - cluster not calibrated (calibrated_threshold IS NULL) → 'llm'
    - similarity below calibrated_threshold → 'llm'
    - similarity meets calibrated_threshold → 'brain'
    - DB failure in _fetch_threshold → safe 'llm' fallback, never raises
    - control_plane's per-flag intent sets are stable (the old
      ROUTING_INELIGIBLE_INTENTS was split out -- see control_plane.py)

Run:
    docker compose exec python-api python -m unittest tests.test_routing_service -v
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.language.calibration_types import RoutingDecision
from app.language import control_plane
from app.language.routing_service import RoutingService
from app.language.shadow_brain import SHADOW_BRAIN_VERSION
from app.language.shadow_brain_types import BrainPrediction


# ── Helpers ────────────────────────────────────────────────────────────

def _prediction(intent: str, similarity: float = 0.85) -> BrainPrediction:
    return BrainPrediction(
        predicted_intent=intent,
        similarity=similarity,
        cluster_id=1,
        matched_example="example",
    )


class FakeRow:
    def __init__(self, calibrated_threshold, agreement_rate):
        self.calibrated_threshold = calibrated_threshold
        self.agreement_rate = agreement_rate


class FakeResult:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class FakeSession:
    """Returns a pre-programmed threshold row (or None) from _fetch_threshold."""

    def __init__(self, threshold=None, agreement_rate=None):
        self._threshold = threshold
        self._agreement_rate = agreement_rate
        self.executed_queries = []

    async def execute(self, query, params=None):
        self.executed_queries.append((str(query), params))
        if self._threshold is not None:
            row = FakeRow(self._threshold, self._agreement_rate)
            return FakeResult(row)
        return FakeResult(None)

    async def commit(self):
        pass


class FailingSession(FakeSession):
    async def execute(self, query, params=None):
        self.executed_queries.append((str(query), params))
        raise RuntimeError("db temporarily unavailable")


# ── Tests ──────────────────────────────────────────────────────────────

class TestRoutingService(unittest.IsolatedAsyncioTestCase):

    # ── No prediction ─────────────────────────────────────────────────

    async def test_no_brain_prediction_returns_shadow(self):
        svc = RoutingService()
        session = FakeSession()
        decision = await svc.decide(session, tenant_id=1, brain_prediction=None)
        self.assertEqual(decision.routed_to, "shadow")
        self.assertIsNone(decision.predicted_intent)
        self.assertEqual(session.executed_queries, [])  # no DB call needed

    # ── Business-risk intents (CHANGED — Phase 4 corrected scope) ─────
    # Routing for UNDERSTANDING is a different control plane from
    # authorizing ACTION (GENERAL_LANGUAGE_BRAIN.md §3/§3.1). These
    # intents are no longer blocked from 'brain' routing: they are
    # calibrated and routed like any other. The autonomous-action gate
    # lives in control_plane.AUTOMATION_INELIGIBLE_INTENTS, for the
    # not-yet-built Tool Engine.

    async def test_create_order_routes_like_any_other_intent_when_calibrated(self):
        svc = RoutingService()
        session = FakeSession(threshold=0.50, agreement_rate=0.99)
        decision = await svc.decide(
            session, tenant_id=1, brain_prediction=_prediction("CREATE_ORDER", similarity=0.99)
        )
        self.assertEqual(decision.routed_to, "brain")
        # Now consults intent_clusters like every other intent.
        self.assertGreaterEqual(len(session.executed_queries), 1)

    async def test_order_status_routes_like_any_other_intent_when_calibrated(self):
        svc = RoutingService()
        session = FakeSession(threshold=0.50, agreement_rate=0.99)
        decision = await svc.decide(
            session, tenant_id=1, brain_prediction=_prediction("ORDER_STATUS", similarity=0.98)
        )
        self.assertEqual(decision.routed_to, "brain")

    async def test_business_risk_intent_still_falls_to_llm_when_uncalibrated(self):
        """Removing the hard block must not remove the calibration gate."""
        svc = RoutingService()
        session = FakeSession(threshold=None, agreement_rate=None)
        decision = await svc.decide(
            session, tenant_id=1, brain_prediction=_prediction("CREATE_ORDER", similarity=0.99)
        )
        self.assertEqual(decision.routed_to, "llm")

    # ── Uncalibrated cluster ──────────────────────────────────────────

    async def test_uncalibrated_cluster_routes_llm(self):
        svc = RoutingService()
        session = FakeSession(threshold=None)  # no calibrated_threshold in DB
        decision = await svc.decide(
            session, tenant_id=1, brain_prediction=_prediction("PRICE_INQUIRY", similarity=0.90)
        )
        self.assertEqual(decision.routed_to, "llm")
        self.assertIsNone(decision.calibrated_threshold)

    # ── Similarity below threshold ────────────────────────────────────

    async def test_similarity_below_threshold_routes_llm(self):
        svc = RoutingService()
        session = FakeSession(threshold=0.80, agreement_rate=0.92)
        decision = await svc.decide(
            session,
            tenant_id=1,
            brain_prediction=_prediction("DELIVERY_INFO", similarity=0.75),  # 0.75 < 0.80
        )
        self.assertEqual(decision.routed_to, "llm")
        self.assertEqual(decision.predicted_intent, "DELIVERY_INFO")
        self.assertAlmostEqual(decision.similarity, 0.75)
        self.assertAlmostEqual(decision.calibrated_threshold, 0.80)

    # ── Similarity meets threshold ────────────────────────────────────

    async def test_similarity_meets_threshold_routes_brain(self):
        svc = RoutingService()
        session = FakeSession(threshold=0.80, agreement_rate=0.93)
        decision = await svc.decide(
            session,
            tenant_id=1,
            brain_prediction=_prediction("PRODUCT_INFO", similarity=0.85),  # 0.85 >= 0.80
        )
        self.assertEqual(decision.routed_to, "brain")
        self.assertEqual(decision.predicted_intent, "PRODUCT_INFO")
        self.assertAlmostEqual(decision.similarity, 0.85)
        self.assertAlmostEqual(decision.calibrated_threshold, 0.80)
        self.assertAlmostEqual(decision.agreement_rate, 0.93)
        self.assertEqual(decision.brain_version, SHADOW_BRAIN_VERSION)

    async def test_similarity_exactly_at_threshold_routes_brain(self):
        svc = RoutingService()
        session = FakeSession(threshold=0.80, agreement_rate=0.91)
        decision = await svc.decide(
            session,
            tenant_id=1,
            brain_prediction=_prediction("COMPLAINT", similarity=0.80),  # exactly at threshold
        )
        self.assertEqual(decision.routed_to, "brain")

    # ── DB failure isolation ──────────────────────────────────────────

    async def test_db_failure_falls_back_to_llm_never_raises(self):
        svc = RoutingService()
        session = FailingSession()
        try:
            decision = await svc.decide(
                session, tenant_id=1, brain_prediction=_prediction("RETURN_REQUEST", 0.88)
            )
        except Exception as exc:
            self.fail(f"decide() raised unexpectedly: {exc}")
        self.assertEqual(decision.routed_to, "llm")

    # ── Decision carries correct metadata ─────────────────────────────

    async def test_brain_route_carries_brain_version(self):
        svc = RoutingService()
        session = FakeSession(threshold=0.70, agreement_rate=0.95)
        decision = await svc.decide(
            session,
            tenant_id=2,
            brain_prediction=_prediction("NEGOTIATION", similarity=0.80),
        )
        self.assertEqual(decision.brain_version, SHADOW_BRAIN_VERSION)

    async def test_shadow_route_carries_brain_version(self):
        svc = RoutingService()
        session = FakeSession()
        decision = await svc.decide(session, tenant_id=1, brain_prediction=None)
        self.assertEqual(decision.brain_version, SHADOW_BRAIN_VERSION)

    async def test_llm_route_on_uncalibrated_carries_prediction_metadata(self):
        svc = RoutingService()
        session = FakeSession(threshold=None)
        decision = await svc.decide(
            session,
            tenant_id=1,
            brain_prediction=_prediction("GENERAL_QUESTION", similarity=0.72),
        )
        self.assertEqual(decision.routed_to, "llm")
        self.assertEqual(decision.predicted_intent, "GENERAL_QUESTION")
        self.assertAlmostEqual(decision.similarity, 0.72)
        self.assertIsNone(decision.calibrated_threshold)

    # ── Constants ─────────────────────────────────────────────────────

    def test_automation_ineligible_intents_are_stable(self):
        """Pin so a change to business-risk exclusions is deliberate.
        (Replaces the removed routing_service.ROUTING_INELIGIBLE_INTENTS.)"""
        self.assertIn("CREATE_ORDER", control_plane.AUTOMATION_INELIGIBLE_INTENTS)
        self.assertIn("ORDER_STATUS", control_plane.AUTOMATION_INELIGIBLE_INTENTS)

    def test_control_plane_sets_are_frozensets(self):
        """Immutable at runtime -- no accidental mutation possible."""
        for s in (
            control_plane.AUTOMATION_INELIGIBLE_INTENTS,
            control_plane.LANGUAGE_LEARNING_INELIGIBLE_INTENTS,
            control_plane.PROMOTION_INELIGIBLE_INTENTS,
            control_plane.TRAINING_INELIGIBLE_INTENTS,
        ):
            self.assertIsInstance(s, frozenset)

    def test_business_risk_intents_still_eligible_for_language_learning(self):
        """§3.1 corrected scope: CREATE_ORDER/ORDER_STATUS are excluded
        from AUTOMATION only -- never from language learning."""
        self.assertTrue(control_plane.is_language_learning_eligible("CREATE_ORDER"))
        self.assertFalse(control_plane.is_automation_eligible("CREATE_ORDER"))


if __name__ == "__main__":
    unittest.main()
