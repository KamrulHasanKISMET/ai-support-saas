"""
Tests for TenantCalibrationConfig (Phase 4).

Run:
    docker compose exec python-api python -m unittest tests.test_tenant_calibration_config -v
"""
import unittest
from app.language.tenant_calibration_config import TenantCalibrationConfig


class FakeRow:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeResult:
    def __init__(self, row=None):
        self._row = row
    def first(self):
        return self._row


class FakeSession:
    def __init__(self, row=None):
        self._row = row
        self.commits = 0
        self.executed = []

    async def execute(self, query, params=None):
        self.executed.append(str(query)[:40])
        return FakeResult(self._row)

    async def commit(self):
        self.commits += 1


class FailingSession(FakeSession):
    async def execute(self, query, params=None):
        raise RuntimeError("db down")


class TestTenantCalibrationConfig(unittest.IsolatedAsyncioTestCase):

    async def test_no_row_returns_all_defaults(self):
        cfg = await TenantCalibrationConfig.load(FakeSession(row=None), tenant_id=1)
        self.assertEqual(cfg.min_agreement_rate, 0.90)
        self.assertEqual(cfg.min_sample_count, 30)
        self.assertEqual(cfg.min_promote_accuracy, 0.95)
        self.assertEqual(cfg.min_rollback_accuracy, 0.85)
        self.assertEqual(cfg.initial_canary_pct, 5.0)
        self.assertAlmostEqual(cfg.novelty_threshold, 0.50)
        self.assertTrue(cfg.novelty_logging_enabled)

    async def test_row_overrides_defaults(self):
        row = FakeRow(
            min_agreement_rate=0.80,
            min_sample_count=20,
            min_promote_accuracy=0.92,
            min_rollback_accuracy=0.78,
            initial_canary_pct=10.0,
            novelty_threshold=0.40,
            novelty_logging_enabled=False,
        )
        cfg = await TenantCalibrationConfig.load(FakeSession(row=row), tenant_id=2)
        self.assertAlmostEqual(cfg.min_agreement_rate, 0.80)
        self.assertEqual(cfg.min_sample_count, 20)
        self.assertAlmostEqual(cfg.min_promote_accuracy, 0.92)
        self.assertAlmostEqual(cfg.min_rollback_accuracy, 0.78)
        self.assertAlmostEqual(cfg.initial_canary_pct, 10.0)
        self.assertAlmostEqual(cfg.novelty_threshold, 0.40)
        self.assertFalse(cfg.novelty_logging_enabled)

    async def test_db_failure_returns_defaults_never_raises(self):
        try:
            cfg = await TenantCalibrationConfig.load(FailingSession(), tenant_id=3)
        except Exception as e:
            self.fail(f"load() raised: {e}")
        self.assertEqual(cfg.min_agreement_rate, 0.90)

    async def test_is_frozen(self):
        cfg = await TenantCalibrationConfig.load(FakeSession(row=None), tenant_id=1)
        with self.assertRaises((AttributeError, TypeError)):
            cfg.min_agreement_rate = 0.70  # type: ignore

    async def test_save_commits(self):
        cfg = TenantCalibrationConfig(tenant_id=5, min_agreement_rate=0.85)
        session = FakeSession()
        await cfg.save(session)
        self.assertGreater(session.commits, 0)

    async def test_tenant_id_preserved(self):
        cfg = await TenantCalibrationConfig.load(FakeSession(row=None), tenant_id=42)
        self.assertEqual(cfg.tenant_id, 42)


if __name__ == "__main__":
    unittest.main()
