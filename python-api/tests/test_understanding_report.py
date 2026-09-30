"""
Tests for app/language/understanding_report.py + its route wiring.
Pure aggregation and a fake session; no Postgres. The route file is
checked structurally (FastAPI is not needed to run these).

Run:
    docker compose exec python-api python -m unittest tests.test_understanding_report -v
"""

import unittest
from pathlib import Path

from app.language import understanding_report as ur

APP = Path(__file__).resolve().parents[1] / "app"


def _counts(**o):
    base = dict(total=100, und_language=0, brain=30, llm=60, automation_ineligible=5,
                code_mixed=20, transliterated=25, ambiguous=4, novel=8, low_confidence=10)
    base.update(o)
    return base


class TestBuildReport(unittest.TestCase):
    def test_rates_and_shares(self):
        r = ur.build_report(_counts(), [("bn", 60), ("en", 40)], [("bengali", 50), ("latin", 50)],
                            tenant_id=1, days=30)
        self.assertEqual(r["total_turns"], 100)
        self.assertEqual(r["phenomena"]["code_mixed"], {"turns": 20, "share": 0.2})
        self.assertEqual(r["by_language"][0], {"value": "bn", "turns": 60, "share": 0.6})
        self.assertEqual(r["automation_ineligible"]["share"], 0.05)

    def test_intent_source_attribution(self):
        r = ur.build_report(_counts(), [], [], tenant_id=1, days=30)
        s = r["intent_source"]
        self.assertEqual((s["brain"], s["llm"], s["unattributed"]), (30, 60, 10))
        self.assertAlmostEqual(s["brain_share_of_attributed"], 30 / 90, places=4)

    def test_empty_window_no_division_by_zero(self):
        r = ur.build_report({}, [], [], tenant_id=1, days=7)
        self.assertEqual(r["total_turns"], 0)
        self.assertIsNone(r["phenomena"]["novel"]["share"])
        self.assertIsNone(r["intent_source"]["brain_share_of_attributed"])
        self.assertIn("no turns in window", r["warnings"])

    def test_small_sample_flagged(self):
        r = ur.build_report(_counts(total=5), [], [], tenant_id=1, days=30)
        self.assertTrue(any("noise" in w for w in r["warnings"]))

    def test_und_language_flagged_factually(self):
        r = ur.build_report(_counts(und_language=3), [], [], tenant_id=1, days=30)
        self.assertTrue(any("language='und'" in w for w in r["warnings"]))
        self.assertEqual(r["language_unresolved"]["turns"], 3)

    def test_clean_report_has_no_warnings(self):
        self.assertEqual(ur.build_report(_counts(), [], [], tenant_id=1, days=30)["warnings"], [])

    def test_top_n_truncation(self):
        rows = [(f"l{i}", 100 - i) for i in range(25)]
        r = ur.build_report(_counts(), rows, [], tenant_id=1, days=30)
        self.assertEqual(len(r["by_language"]), ur.TOP_N)

    def test_no_judgement_words(self):
        r = str(ur.build_report(_counts(), [], [], tenant_id=1, days=30)).lower()
        for w in ("good", "bad", "healthy", "unhealthy"):
            self.assertNotIn(w, r)


class Row:
    def __init__(self, **kw): self.__dict__.update(kw)


class FakeResult:
    def __init__(self, first=None, rows=None): self._f, self._r = first, rows or []
    def first(self): return self._f
    def all(self): return self._r


class FakeSession:
    def __init__(self):
        self.calls = []
    async def execute(self, q, params=None):
        self.calls.append((str(q), params))
        if "COUNT(*)  " in str(q) or "AS total" in str(q):
            return FakeResult(first=Row(**_counts()))
        return FakeResult(rows=[Row(v="bn", n=60), Row(v="en", n=40)])


class TestFetch(unittest.IsolatedAsyncioTestCase):
    async def test_every_query_is_tenant_scoped(self):
        s = FakeSession()
        r = await ur.fetch(s, tenant_id=7, days=30)
        self.assertEqual(len(s.calls), 3)
        for q, p in s.calls:
            self.assertIn("tenant_id = :t", q)
            self.assertEqual(p["t"], 7)
        self.assertEqual(r["tenant_id"], 7)
        self.assertEqual(r["by_language"][0]["value"], "bn")

    async def test_days_clamped_to_retention(self):
        s = FakeSession()
        r = await ur.fetch(s, tenant_id=1, days=9999)
        self.assertEqual(r["window_days"], 90)
        self.assertEqual(s.calls[0][1]["d"], 90)
        r = await ur.fetch(FakeSession(), tenant_id=1, days=0)
        self.assertEqual(r["window_days"], 1)

    async def test_reads_no_message_text_columns(self):
        s = FakeSession()
        await ur.fetch(s, tenant_id=1, days=30)
        joined = " ".join(q for q, _ in s.calls).lower()
        self.assertNotIn("message", joined)


class TestRouteWiring(unittest.TestCase):
    def test_route_is_secret_gated_and_tenant_scoped(self):
        src = (APP / "api/routes/understanding.py").read_text(encoding="utf-8")
        self.assertIn("Depends(require_internal_secret)", src)
        self.assertIn('alias="tenantId"', src)
        self.assertIn("Query(..., alias", src)   # tenantId is required, no default

    def test_route_registered_in_main(self):
        src = (APP / "main.py").read_text(encoding="utf-8")
        self.assertIn("understanding.router", src)

    def test_days_bounded_at_retention(self):
        src = (APP / "api/routes/understanding.py").read_text(encoding="utf-8")
        self.assertIn("le=90", src)


if __name__ == "__main__":
    unittest.main()
