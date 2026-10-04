"""
Tests for app/api/routes/admin_novelty.py.

Pure validation + handlers driven through a scripted fake session. No
Postgres, and no real FastAPI/pydantic (those are stubbed by the
sandbox runner), so NOT exercised here: real request parsing,
`extra="forbid"`, and the SQL itself against Postgres -- hit the routes
once in Docker.

Run:
    docker compose exec python-api python -m unittest tests.test_admin_novelty -v
"""

import asyncio
import unittest
import uuid
from datetime import datetime
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.routes import admin_novelty as an
from app.intent.intent_types import IntentType

NOW = datetime(2026, 9, 28, 12, 0, 0)


def run(coro):
    return asyncio.run(coro)


def event_row(**over):
    base = dict(
        id=1, conversation_id=10, experience_id=uuid.UUID(int=1), request_id="req-1",
        normalized_message="kichu ekta", best_similarity=0.31, best_intent="GENERAL_QUESTION",
        novelty_threshold=0.5, brain_version="v1", triaged=False, triage_result=None,
        triage_intent=None, triaged_at=None, triaged_by=None, created_at=NOW,
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class ScriptedSession:
    """Each execute() pops the next scripted list of rows."""

    def __init__(self, script):
        self.script = list(script)
        self.executed = []
        self.commits = 0

    async def execute(self, q, p=None):
        self.executed.append((str(q), p))
        return FakeResult(self.script.pop(0))

    async def commit(self):
        self.commits += 1


def body(**kw):
    kw.setdefault("triagedBy", "ops@example.com")
    return an.TriageBody(**kw)


class TestValidateTriage(unittest.TestCase):
    def ok(self, **kw):
        clean, errors = an.validate_triage(kw)
        self.assertEqual(errors, [], errors)
        return clean

    def bad(self, **kw):
        clean, errors = an.validate_triage(kw)
        self.assertIsNone(clean)
        self.assertTrue(errors)
        return errors

    def test_existing_intent_ok(self):
        c = self.ok(triageResult="existing_intent", triageIntent="ORDER_STATUS", triagedBy="a")
        self.assertEqual(c, {"triage_result": "existing_intent",
                             "triage_intent": "ORDER_STATUS", "triaged_by": "a"})

    def test_existing_intent_requires_intent(self):
        errs = self.bad(triageResult="existing_intent", triagedBy="a")
        self.assertTrue(any("required" in e for e in errs))

    def test_existing_intent_must_be_known(self):
        self.bad(triageResult="existing_intent", triageIntent="NOT_A_REAL_INTENT", triagedBy="a")

    def test_every_real_intent_accepted_for_existing(self):
        for i in IntentType:
            with self.subTest(i.value):
                self.ok(triageResult="existing_intent", triageIntent=i.value, triagedBy="a")

    def test_new_intent_without_label_ok(self):
        c = self.ok(triageResult="new_intent", triagedBy="a")
        self.assertIsNone(c["triage_intent"])

    def test_new_intent_with_label_ok(self):
        c = self.ok(triageResult="new_intent", triageIntent="WARRANTY_CLAIM", triagedBy="a")
        self.assertEqual(c["triage_intent"], "WARRANTY_CLAIM")

    def test_new_intent_label_colliding_with_existing_rejected(self):
        errs = self.bad(triageResult="new_intent", triageIntent="COMPLAINT", triagedBy="a")
        self.assertTrue(any("existing_intent" in e for e in errs))

    def test_new_intent_label_format(self):
        for label in ("warranty", "Warranty Claim", "W", "1BAD", "A" * 101):
            with self.subTest(label):
                self.bad(triageResult="new_intent", triageIntent=label, triagedBy="a")

    def test_noise_and_spam_must_not_carry_intent(self):
        for r in ("noise", "spam"):
            with self.subTest(r):
                self.ok(triageResult=r, triagedBy="a")
                self.bad(triageResult=r, triageIntent="ORDER_STATUS", triagedBy="a")

    def test_blank_intent_treated_as_absent(self):
        c = self.ok(triageResult="noise", triageIntent="   ", triagedBy="a")
        self.assertIsNone(c["triage_intent"])

    def test_unknown_result_rejected(self):
        self.bad(triageResult="maybe", triagedBy="a")
        self.bad(triageResult=None, triagedBy="a")

    def test_triaged_by_required_trimmed_and_capped(self):
        self.bad(triageResult="noise", triagedBy="")
        self.bad(triageResult="noise", triagedBy="   ")
        self.bad(triageResult="noise", triagedBy=None)
        self.bad(triageResult="noise", triagedBy="x" * 101)
        c = self.ok(triageResult="noise", triagedBy="  ops  ")
        self.assertEqual(c["triaged_by"], "ops")

    def test_results_match_migration_comment(self):
        self.assertEqual(set(an.TRIAGE_RESULTS),
                         {"new_intent", "existing_intent", "noise", "spam"})

    def test_all_errors_reported_together(self):
        _, errs = an.validate_triage({"triageResult": "bogus", "triagedBy": ""})
        self.assertEqual(len(errs), 2)


class TestValidatePaging(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(an.validate_paging("untriaged", 50, 0), [])
        self.assertEqual(an.validate_paging("all", an.MAX_LIMIT, 500), [])

    def test_invalid(self):
        self.assertTrue(an.validate_paging("bogus", 50, 0))
        self.assertTrue(an.validate_paging("all", 0, 0))
        self.assertTrue(an.validate_paging("all", an.MAX_LIMIT + 1, 0))
        self.assertTrue(an.validate_paging("all", 10, -1))

    def test_every_status_has_a_sql_clause_and_only_those(self):
        self.assertEqual(set(an._STATUS_CLAUSE), set(an.STATUSES))


class TestListHandler(unittest.TestCase):
    def test_default_view_is_untriaged_and_tenant_scoped(self):
        db = ScriptedSession([[SimpleNamespace(n=2)], [event_row(id=2), event_row(id=1)]])
        out = run(an.list_novelty_events(5, "untriaged", 50, 0, db))
        count_sql, count_params = db.executed[0]
        list_sql, list_params = db.executed[1]
        self.assertIn("triaged = FALSE", count_sql)
        self.assertIn("triaged = FALSE", list_sql)
        self.assertEqual(count_params["tenant_id"], 5)
        self.assertEqual(list_params, {"tenant_id": 5, "limit": 50, "offset": 0})
        self.assertIn("ORDER BY created_at DESC, id DESC", list_sql)
        self.assertEqual(out["total"], 2)
        self.assertEqual([i["id"] for i in out["items"]], [2, 1])

    def test_triaged_and_all_filters(self):
        db = ScriptedSession([[SimpleNamespace(n=0)], []])
        run(an.list_novelty_events(1, "triaged", 10, 0, db))
        self.assertIn("triaged = TRUE", db.executed[1][0])
        db = ScriptedSession([[SimpleNamespace(n=0)], []])
        run(an.list_novelty_events(1, "all", 10, 0, db))
        self.assertNotIn("triaged =", db.executed[1][0])

    def test_paging_passed_through(self):
        db = ScriptedSession([[SimpleNamespace(n=120)], []])
        out = run(an.list_novelty_events(1, "all", 25, 50, db))
        self.assertEqual(db.executed[1][1]["limit"], 25)
        self.assertEqual(db.executed[1][1]["offset"], 50)
        self.assertEqual((out["limit"], out["offset"], out["total"]), (25, 50, 120))

    def test_invalid_params_422_and_no_query(self):
        db = ScriptedSession([])
        for args in (("bogus", 50, 0), ("all", 0, 0), ("all", 101, 0), ("all", 10, -5)):
            with self.subTest(args):
                with self.assertRaises(HTTPException) as cm:
                    run(an.list_novelty_events(1, *args, db))
                self.assertEqual(cm.exception.status_code, 422)
        self.assertEqual(db.executed, [])

    def test_item_shape_and_null_handling(self):
        db = ScriptedSession([[SimpleNamespace(n=1)],
                              [event_row(best_similarity=None, best_intent=None, experience_id=None)]])
        item = run(an.list_novelty_events(1, "all", 10, 0, db))["items"][0]
        self.assertIsNone(item["bestSimilarity"])
        self.assertIsNone(item["experienceId"])
        self.assertEqual(item["createdAt"], NOW.isoformat())
        self.assertEqual(item["normalizedMessage"], "kichu ekta")
        self.assertFalse(item["triaged"])

    def test_empty_result(self):
        db = ScriptedSession([[SimpleNamespace(n=0)], []])
        out = run(an.list_novelty_events(1, "untriaged", 50, 0, db))
        self.assertEqual((out["total"], out["items"]), (0, []))


class TestTriageHandler(unittest.TestCase):
    def prev(self, **o):
        base = dict(triaged=False, triage_result=None, triage_intent=None, triaged_by=None)
        base.update(o)
        return SimpleNamespace(**base)

    def test_success_updates_and_commits(self):
        after = event_row(triaged=True, triage_result="existing_intent",
                          triage_intent="ORDER_STATUS", triaged_at=NOW, triaged_by="ops@example.com")
        db = ScriptedSession([[self.prev()], [after]])
        out = run(an.triage_novelty_event(
            1, body(triageResult="existing_intent", triageIntent="ORDER_STATUS"), 5, db))
        update_sql, params = db.executed[1]
        self.assertIn("UPDATE novelty_events", update_sql)
        self.assertIn("tenant_id = :tenant_id", update_sql)
        self.assertEqual(params["tenant_id"], 5)
        self.assertEqual(params["event_id"], 1)
        self.assertEqual(params["triage_result"], "existing_intent")
        self.assertEqual(params["triage_intent"], "ORDER_STATUS")
        self.assertEqual(params["triaged_by"], "ops@example.com")
        self.assertEqual(db.commits, 1)
        self.assertTrue(out["triaged"])
        self.assertEqual(out["triageIntent"], "ORDER_STATUS")

    def test_lookup_is_tenant_scoped(self):
        db = ScriptedSession([[self.prev()], [event_row(triaged=True)]])
        run(an.triage_novelty_event(1, body(triageResult="noise"), 9, db))
        sql, params = db.executed[0]
        self.assertIn("tenant_id = :tenant_id", sql)
        self.assertEqual(params["tenant_id"], 9)

    def test_other_tenants_event_is_404_and_nothing_written(self):
        db = ScriptedSession([[]])
        with self.assertRaises(HTTPException) as cm:
            run(an.triage_novelty_event(1, body(triageResult="noise"), 2, db))
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(len(db.executed), 1)
        self.assertEqual(db.commits, 0)

    def test_invalid_verdict_422_before_any_db_call(self):
        db = ScriptedSession([])
        with self.assertRaises(HTTPException) as cm:
            run(an.triage_novelty_event(
                1, body(triageResult="existing_intent"), 5, db))  # intent missing
        self.assertEqual(cm.exception.status_code, 422)
        self.assertEqual(db.executed, [])

    def test_row_vanishing_between_statements_is_404(self):
        db = ScriptedSession([[self.prev()], []])
        with self.assertRaises(HTTPException) as cm:
            run(an.triage_novelty_event(1, body(triageResult="spam"), 5, db))
        self.assertEqual(cm.exception.status_code, 404)

    def test_retriage_allowed_overwrites(self):
        db = ScriptedSession([
            [self.prev(triaged=True, triage_result="noise")],
            [event_row(triaged=True, triage_result="spam", triaged_at=NOW, triaged_by="ops@example.com")],
        ])
        out = run(an.triage_novelty_event(1, body(triageResult="spam"), 5, db))
        self.assertEqual(out["triageResult"], "spam")

    def test_noise_writes_null_intent(self):
        db = ScriptedSession([[self.prev()], [event_row(triaged=True, triage_result="noise")]])
        run(an.triage_novelty_event(1, body(triageResult="noise"), 5, db))
        self.assertIsNone(db.executed[1][1]["triage_intent"])


class TestWiring(unittest.TestCase):
    def test_routes_registered(self):
        got = {(m, p) for m, p, _ in an.router.routes}
        self.assertIn(("GET", "/tenants/{tenant_id}/novelty-events"), got)
        self.assertIn(("PATCH", "/novelty-events/{event_id}"), got)

    def test_router_gated_and_prefixed(self):
        self.assertEqual(an.router.prefix, "/admin")
        self.assertTrue(an.router.dependencies)

    def test_body_model_fields(self):
        self.assertEqual(set(an.TriageBody.__annotations__),
                         {"triageResult", "triageIntent", "triagedBy"})


if __name__ == "__main__":
    unittest.main()
