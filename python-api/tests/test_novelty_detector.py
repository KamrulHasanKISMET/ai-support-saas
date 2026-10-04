"""
Tests for NoveltyDetector (Phase 4).

Run:
    docker compose exec python-api python -m unittest tests.test_novelty_detector -v
"""
import uuid
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

from app.language.novelty_detector import NoveltyDetector
from app.language.shadow_brain_types import BrainPrediction
from app.language.tenant_calibration_config import TenantCalibrationConfig


def _pred(similarity: float, intent: str = "PRICE_INQUIRY") -> BrainPrediction:
    return BrainPrediction(
        predicted_intent=intent,
        similarity=similarity,
        cluster_id=1,
        matched_example="example",
    )


EXP_ID = uuid.uuid4()
BASE_ARGS = dict(
    tenant_id=1, customer_id=1, conversation_id=1,
    experience_id=EXP_ID, request_id="req-1",
    normalized_message="test message",
)


class FakeResult:
    def first(self): return None


class FakeSession:
    def __init__(self):
        self.executed = []
        self.commits = 0
    async def execute(self, q, p=None):
        self.executed.append(str(q)[:50])
        return FakeResult()
    async def commit(self):
        self.commits += 1


class FailingSession(FakeSession):
    async def execute(self, q, p=None):
        raise RuntimeError("db down")


def _cfg(threshold=0.50, logging_enabled=True):
    return TenantCalibrationConfig(
        tenant_id=1,
        novelty_threshold=threshold,
        novelty_logging_enabled=logging_enabled,
    )


class TestNoveltyDetector(unittest.IsolatedAsyncioTestCase):

    async def _run(self, session, brain_prediction, cfg):
        det = NoveltyDetector()
        with patch(
            "app.language.novelty_detector.TenantCalibrationConfig.load",
            new=AsyncMock(return_value=cfg),
        ):
            return await det.check_and_log(session, brain_prediction=brain_prediction, **BASE_ARGS)

    async def test_no_prediction_is_novel(self):
        session = FakeSession()
        result = await self._run(session, None, _cfg(threshold=0.50))
        self.assertTrue(result)
        self.assertGreater(session.commits, 0)

    async def test_similarity_below_threshold_is_novel(self):
        session = FakeSession()
        result = await self._run(session, _pred(0.40), _cfg(threshold=0.50))
        self.assertTrue(result)
        self.assertGreater(session.commits, 0)

    async def test_similarity_above_threshold_not_novel(self):
        session = FakeSession()
        result = await self._run(session, _pred(0.80), _cfg(threshold=0.50))
        self.assertFalse(result)
        self.assertEqual(session.commits, 0)

    async def test_similarity_exactly_at_threshold_not_novel(self):
        """At threshold = not novel (strictly less than triggers novelty)."""
        session = FakeSession()
        result = await self._run(session, _pred(0.50), _cfg(threshold=0.50))
        self.assertFalse(result)

    async def test_logging_disabled_skips_write(self):
        session = FakeSession()
        result = await self._run(session, _pred(0.10), _cfg(logging_enabled=False))
        self.assertFalse(result)
        self.assertEqual(session.commits, 0)

    async def test_db_failure_returns_false_never_raises(self):
        session = FailingSession()
        try:
            result = await self._run(session, _pred(0.20), _cfg())
        except Exception as e:
            self.fail(f"check_and_log raised: {e}")
        self.assertFalse(result)

    async def test_per_tenant_threshold_respected(self):
        """Higher threshold catches more messages as novel."""
        session = FakeSession()
        # similarity=0.60, threshold=0.70 → novel
        result = await self._run(session, _pred(0.60), _cfg(threshold=0.70))
        self.assertTrue(result)

        session2 = FakeSession()
        # similarity=0.60, threshold=0.50 → not novel
        result2 = await self._run(session2, _pred(0.60), _cfg(threshold=0.50))
        self.assertFalse(result2)



class TestNoveltyOutcome(unittest.IsolatedAsyncioTestCase):
    """check() separates 'is it novel' from 'did we write a row' (C2 in
    docs/PENDING_WORK.md): glb.TurnUnderstanding needs the former."""

    async def _check(self, session, pred, cfg):
        det = NoveltyDetector()
        with patch(
            "app.language.novelty_detector.TenantCalibrationConfig.load",
            new=AsyncMock(return_value=cfg),
        ):
            return await det.check(session, brain_prediction=pred, **BASE_ARGS)

    async def test_novel_and_logged(self):
        out = await self._check(FakeSession(), _pred(0.10), _cfg())
        self.assertTrue(out.is_novel)
        self.assertTrue(out.logged)

    async def test_not_novel_not_logged(self):
        out = await self._check(FakeSession(), _pred(0.90), _cfg())
        self.assertFalse(out.is_novel)
        self.assertFalse(out.logged)

    async def test_novel_but_logging_disabled_still_reports_novel(self):
        session = FakeSession()
        out = await self._check(session, _pred(0.10), _cfg(logging_enabled=False))
        self.assertTrue(out.is_novel)   # the bug this fixes: was invisible
        self.assertFalse(out.logged)
        self.assertEqual(session.commits, 0)

    async def test_db_failure_reports_unknown_not_false(self):
        out = await self._check(FailingSession(), _pred(0.10), _cfg())
        self.assertIsNone(out.is_novel)
        self.assertFalse(out.logged)

    async def test_check_and_log_wrapper_matches_logged(self):
        det = NoveltyDetector()
        with patch(
            "app.language.novelty_detector.TenantCalibrationConfig.load",
            new=AsyncMock(return_value=_cfg(logging_enabled=False)),
        ):
            self.assertFalse(await det.check_and_log(FakeSession(), brain_prediction=_pred(0.1), **BASE_ARGS))


if __name__ == "__main__":
    unittest.main()
