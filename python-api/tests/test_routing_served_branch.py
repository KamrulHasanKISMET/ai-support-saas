"""
Tests for PENDING_WORK C8: routing_decisions records WHICH cluster served a
turn (db/init/019_routing_served_branch.sql), so the canary ramp counts
candidate-served turns only.

Fake sessions, no Postgres. Run:
    docker compose exec python-api python -m unittest tests.test_routing_served_branch -v
"""

import re
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.language import canary_ramp_service as crs
from app.language.calibration_types import RoutingDecision
from app.language.routing_service import (
    BRANCH_CANDIDATE, BRANCH_OTHER, BRANCH_PROMOTED,
    RoutingService, ServingCluster,
)
from app.language.shadow_brain_types import BrainPrediction

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "init" / "019_routing_served_branch.sql"


def _pred(intent="COMPLAINT", sim=0.95):
    return BrainPrediction(predicted_intent=intent, similarity=sim,
                           cluster_id=1, matched_example="x")


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Res:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class RoutedSession:
    """Answers the canary query and the fallback cluster query separately."""

    def __init__(self, canary=None, cluster=None):
        self.canary, self.cluster = canary, cluster

    async def execute(self, query, params=None):
        q = str(query)
        if "FROM canary_splits" in q:
            return Res(self.canary)
        if "FROM intent_clusters" in q:
            return Res(self.cluster)
        raise AssertionError("unexpected query: " + q[:60])

    async def commit(self):
        pass


CANARY = Row(canary_pct=20.0, candidate_cluster_id=77,
             cand_threshold=0.60, cand_agreement=0.91)
PROMOTED = Row(id=5, is_promoted=True, calibrated_threshold=0.70, agreement_rate=0.95)
SEED = Row(id=3, is_promoted=False, calibrated_threshold=0.80, agreement_rate=0.90)


class TestServingBranch(unittest.IsolatedAsyncioTestCase):

    async def test_canary_branch_taken_records_candidate_and_its_cluster(self):
        with patch("app.language.routing_service.random.random", return_value=0.0):
            d = await RoutingService().decide(
                RoutedSession(CANARY, PROMOTED), 1, _pred())
        self.assertEqual(d.routed_to, "brain")
        self.assertEqual(d.served_branch, BRANCH_CANDIDATE)
        self.assertEqual(d.served_cluster_id, 77)
        self.assertEqual(d.calibrated_threshold, 0.60)  # candidate's, not promoted's

    async def test_canary_branch_not_taken_records_promoted(self):
        with patch("app.language.routing_service.random.random", return_value=0.99):
            d = await RoutingService().decide(
                RoutedSession(CANARY, PROMOTED), 1, _pred())
        self.assertEqual(d.served_branch, BRANCH_PROMOTED)
        self.assertEqual(d.served_cluster_id, 5)
        self.assertEqual(d.calibrated_threshold, 0.70)

    async def test_no_canary_promoted_cluster(self):
        d = await RoutingService().decide(RoutedSession(None, PROMOTED), 1, _pred())
        self.assertEqual((d.served_branch, d.served_cluster_id), (BRANCH_PROMOTED, 5))

    async def test_no_canary_unpromoted_cluster_is_other(self):
        d = await RoutingService().decide(RoutedSession(None, SEED), 1, _pred())
        self.assertEqual((d.served_branch, d.served_cluster_id), (BRANCH_OTHER, 3))

    async def test_uncalibrated_records_no_serving_cluster(self):
        d = await RoutingService().decide(RoutedSession(None, None), 1, _pred())
        self.assertEqual(d.routed_to, "llm")
        self.assertIsNone(d.served_branch)
        self.assertIsNone(d.served_cluster_id)

    async def test_below_threshold_llm_still_says_which_cluster_decided(self):
        d = await RoutingService().decide(
            RoutedSession(None, PROMOTED), 1, _pred(sim=0.10))
        self.assertEqual(d.routed_to, "llm")
        self.assertEqual(d.served_cluster_id, 5)

    async def test_no_prediction_shadow_has_no_serving_fields(self):
        d = await RoutingService().decide(RoutedSession(), 1, None)
        self.assertEqual(d.routed_to, "shadow")
        self.assertIsNone(d.served_branch)

    async def test_legacy_two_tuple_view_is_unchanged(self):
        thr, agr = await RoutingService()._fetch_threshold(
            RoutedSession(None, PROMOTED), 1, "COMPLAINT")
        self.assertEqual((thr, agr), (0.70, 0.95))

    async def test_rows_without_id_columns_degrade_to_none_not_crash(self):
        bare = Row(calibrated_threshold=0.7, agreement_rate=0.9)  # old-style fake
        s = await RoutingService()._fetch_serving(RoutedSession(None, bare), 1, "X")
        self.assertEqual(s.threshold, 0.7)
        self.assertIsNone(s.cluster_id)
        self.assertEqual(s.branch, BRANCH_OTHER)

    def test_serving_cluster_defaults_are_all_none(self):
        self.assertEqual(ServingCluster(), ServingCluster(None, None, None, None))

    def test_routing_decision_new_fields_default_none_and_are_keyword_safe(self):
        d = RoutingDecision(routed_to="llm")
        self.assertIsNone(d.served_cluster_id)
        self.assertIsNone(d.served_branch)


class TestPersistence(unittest.IsolatedAsyncioTestCase):

    async def test_core_agent_insert_writes_served_columns(self):
        from app.agent import core_agent
        fn = next(getattr(core_agent, n) for n in dir(core_agent)
                  if "routing" in n.lower() and n.startswith("_")
                  and callable(getattr(core_agent, n)))
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        dec = RoutingDecision(routed_to="brain", predicted_intent="COMPLAINT",
                              similarity=0.9, served_cluster_id=77,
                              served_branch="candidate")
        await fn(db, tenant_id=1, customer_id=2, conversation_id=3,
                 message_id=4, request_id="r", experience_id=uuid.uuid4(),
                 decision=dec, final_intent="COMPLAINT")
        sql, params = db.execute.call_args[0]
        self.assertIn("served_cluster_id", str(sql))
        self.assertIn("served_branch", str(sql))
        self.assertEqual(params["served_cluster_id"], 77)
        self.assertEqual(params["served_branch"], "candidate")
        self.assertTrue(params["was_correct"])

    async def test_core_agent_insert_tolerates_a_decision_without_the_fields(self):
        from app.agent import core_agent
        fn = next(getattr(core_agent, n) for n in dir(core_agent)
                  if "routing" in n.lower() and n.startswith("_")
                  and callable(getattr(core_agent, n)))
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        legacy = MagicMock(spec=["routed_to", "predicted_intent", "similarity",
                                 "calibrated_threshold", "agreement_rate",
                                 "brain_version"])
        legacy.routed_to, legacy.predicted_intent = "llm", None
        legacy.similarity = legacy.calibrated_threshold = None
        legacy.agreement_rate = legacy.brain_version = None
        await fn(db, tenant_id=1, customer_id=2, conversation_id=3,
                 message_id=4, request_id="r", experience_id=uuid.uuid4(),
                 decision=legacy, final_intent=None)
        _, params = db.execute.call_args[0]
        self.assertIsNone(params["served_cluster_id"])
        self.assertIsNone(params["served_branch"])


class TestMigrationAgreesWithCode(unittest.TestCase):

    def setUp(self):
        self.sql = MIGRATION.read_text()

    def test_migration_exists_and_is_idempotent(self):
        self.assertIn("ADD COLUMN IF NOT EXISTS served_cluster_id", self.sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS served_branch", self.sql)
        self.assertIn("CREATE INDEX IF NOT EXISTS", self.sql)

    def test_check_constraint_lists_exactly_the_code_branches(self):
        m = re.search(r"IN \(([^)]*)\)\)", self.sql)
        self.assertIsNotNone(m)
        vals = set(re.findall(r"'([a-z]+)'", m.group(1)))
        self.assertEqual(vals, {BRANCH_CANDIDATE, BRANCH_PROMOTED, BRANCH_OTHER})

    def test_ramp_query_uses_columns_the_migration_adds(self):
        for col in ("served_branch", "served_cluster_id"):
            self.assertIn(col, crs._STAGE_STATS)
            self.assertIn(col, self.sql)
        self.assertIn("'candidate'", crs._STAGE_STATS)

    def test_no_older_migration_was_edited_to_add_the_columns(self):
        for f in (REPO / "db" / "init").glob("0*.sql"):
            if f.name != MIGRATION.name:
                self.assertNotIn("served_branch", f.read_text(), f.name)

    def test_column_width_fits_longest_branch(self):
        m = re.search(r"served_branch\s+VARCHAR\((\d+)\)", self.sql)
        self.assertGreaterEqual(int(m.group(1)),
                                max(len(BRANCH_CANDIDATE), len(BRANCH_PROMOTED),
                                    len(BRANCH_OTHER)))


if __name__ == "__main__":
    unittest.main()
