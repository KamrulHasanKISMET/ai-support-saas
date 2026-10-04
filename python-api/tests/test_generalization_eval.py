"""
Tests for app/language/generalization_eval.py (§5.5 generalization test).

These test the MEASURING INSTRUMENT with fakes (deterministic embed /
normalize). They say nothing about how well the real Brain generalizes --
that needs `python -m app.language.generalization_eval --tenant N`
against real clusters, a real embedding provider and the real LLM.

Run:
    docker compose exec python-api python -m unittest tests.test_generalization_eval -v
"""

import unittest

from app.intent.intent_types import IntentType
from app.language.generalization_eval import (
    ClusterPoint,
    cosine,
    evaluate,
    format_report,
    load_eval_set,
    nearest_intent,
    passes_gate,
)

CASES = load_eval_set()
INTENTS = sorted({c.intent for c in CASES})
AXIS = {intent: i for i, intent in enumerate(INTENTS)}


def _vec(intent):
    v = [0.0] * len(INTENTS)
    v[AXIS[intent]] = 1.0
    return v


POINTS = [ClusterPoint(i, _vec(i), f"seed example for {i}") for i in INTENTS]
BY_TEXT = {c.text: c for c in CASES}


async def perfect_normalize(text):
    return f"<{BY_TEXT[text].intent}>"


async def embed(norm):  # "<INTENT>" -> that intent's axis
    return _vec(norm.strip("<>"))


class TestDataset(unittest.TestCase):
    def test_ids_and_texts_unique(self):
        self.assertEqual(len({c.id for c in CASES}), len(CASES))
        self.assertEqual(len({c.text for c in CASES}), len(CASES))

    def test_intents_are_real_intent_types(self):
        valid = {i.value for i in IntentType}
        self.assertTrue({c.intent for c in CASES} <= valid)

    def test_broad_language_and_script_coverage(self):
        langs = {c.language for c in CASES}
        scripts = {c.script for c in CASES}
        self.assertGreaterEqual(len(langs), 10)
        self.assertGreaterEqual(len(scripts), 8)
        self.assertTrue({"hi", "ar", "es", "zh", "ko", "th", "ru", "tr", "bn", "en"} <= langs)

    def test_each_language_has_enough_cases_for_the_gate(self):
        for lang in {c.language for c in CASES}:
            self.assertGreaterEqual(sum(1 for c in CASES if c.language == lang), 5, lang)

    def test_every_concept_spans_the_same_variants(self):
        by_concept = {}
        for c in CASES:
            by_concept.setdefault(c.concept_id, set()).add((c.language, c.script, c.transliterated))
        first = next(iter(by_concept.values()))
        for cid, variants in by_concept.items():
            self.assertEqual(variants, first, cid)

    def test_transliterated_cases_are_latin_script(self):
        tr = [c for c in CASES if c.transliterated]
        self.assertTrue(tr)
        self.assertTrue(all(c.script == "latin" for c in tr))

    def test_each_concept_has_a_single_intent(self):
        m = {}
        for c in CASES:
            m.setdefault(c.concept_id, set()).add(c.intent)
        self.assertTrue(all(len(v) == 1 for v in m.values()))


class TestMath(unittest.TestCase):
    def test_cosine_and_nearest(self):
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine([0, 0], [1, 0]), 0.0)
        intent, sim = nearest_intent([0.9, 0.1], [ClusterPoint("A", [1, 0]), ClusterPoint("B", [0, 1])])
        self.assertEqual(intent, "A")
        self.assertGreater(sim, 0.9)
        self.assertEqual(nearest_intent([1, 0], []), (None, None))


class TestEvaluate(unittest.IsolatedAsyncioTestCase):
    async def test_perfect_generalization_passes_gate(self):
        r = await evaluate(CASES, POINTS, perfect_normalize, embed)
        self.assertEqual(r.overall.n, len(CASES))
        self.assertEqual(r.overall.accuracy, 1.0)
        self.assertEqual(r.concept_consistency, 1.0)
        ok, reasons = passes_gate(r)
        self.assertTrue(ok, reasons)

    async def test_one_failing_language_is_caught_even_when_overall_looks_fine(self):
        """The reason slices exist: Thai fails completely, overall stays
        ~92%, and the gate must still FAIL naming Thai."""
        async def thai_broken(text):
            c = BY_TEXT[text]
            wrong = INTENTS[(AXIS[c.intent] + 1) % len(INTENTS)]
            return f"<{wrong if c.language == 'th' else c.intent}>"

        r = await evaluate(CASES, POINTS, thai_broken, embed)
        self.assertGreater(r.overall.accuracy, 0.85)
        self.assertEqual(r.by_language["th"].accuracy, 0.0)
        ok, reasons = passes_gate(r)
        self.assertFalse(ok)
        self.assertTrue(any(x.startswith("th:") for x in reasons), reasons)

    async def test_filtering_cases_by_intent_scopes_the_report(self):
        """
        PromotionService's generalization gate (promotion_service.py
        _run_generalization_gate) scores one intent at a time by
        filtering the case list before calling evaluate() -- the same
        thing run_for_tenant(..., intent=...) does internally. Confirm
        the filtered report only reflects that intent's own cases.
        """
        one_intent = INTENTS[0]
        filtered = [c for c in CASES if c.intent == one_intent]
        self.assertLess(len(filtered), len(CASES))  # actually a subset

        r = await evaluate(filtered, POINTS, perfect_normalize, embed)
        self.assertEqual(r.overall.n, len(filtered))
        self.assertEqual(r.overall.accuracy, 1.0)
        # Every scored case belongs to the requested intent.
        self.assertTrue(all(res.case.intent == one_intent for res in r.results))

    async def test_transliterated_slice_reported_separately(self):
        async def translit_broken(text):
            c = BY_TEXT[text]
            wrong = INTENTS[(AXIS[c.intent] + 1) % len(INTENTS)]
            return f"<{wrong if c.transliterated else c.intent}>"

        r = await evaluate(CASES, POINTS, translit_broken, embed)
        self.assertEqual(r.native.accuracy, 1.0)
        self.assertEqual(r.transliterated.accuracy, 0.0)

    async def test_leaked_cases_are_skipped_not_scored(self):
        leaky = POINTS + [ClusterPoint("PRICE_INQUIRY", _vec("PRICE_INQUIRY"), "How much does this cost?")]
        r = await evaluate(CASES, leaky, perfect_normalize, embed)
        self.assertIn("price_how_much.en", r.skipped_leaked)
        self.assertEqual(r.overall.n, len(CASES) - 1)

    async def test_leak_detected_after_normalization_too(self):
        leaky = POINTS + [ClusterPoint("ORDER_STATUS", _vec("ORDER_STATUS"), "<ORDER_STATUS>")]
        r = await evaluate(CASES, leaky, perfect_normalize, embed)
        # every ORDER_STATUS case normalizes to the leaked string
        self.assertEqual(sum(1 for c in CASES if c.intent == "ORDER_STATUS"), len(r.skipped_leaked))

    async def test_a_failing_case_is_reported_not_fatal(self):
        async def flaky(text):
            if BY_TEXT[text].id == "order_where.th":
                raise RuntimeError("llm down")
            return await perfect_normalize(text)

        r = await evaluate(CASES, POINTS, flaky, embed)
        self.assertEqual(r.skipped_error, ["order_where.th"])
        self.assertEqual(r.overall.n, len(CASES) - 1)

    async def test_insufficient_language_coverage_fails_gate(self):
        few = [c for c in CASES if c.language != "th"] + [c for c in CASES if c.language == "th"][:2]
        r = await evaluate(few, POINTS, perfect_normalize, embed)
        ok, reasons = passes_gate(r)
        self.assertFalse(ok)
        self.assertTrue(any("th" in x and "insufficient coverage" in x for x in reasons), reasons)

    async def test_no_scored_cases_fails_gate(self):
        r = await evaluate([], POINTS, perfect_normalize, embed)
        ok, reasons = passes_gate(r)
        self.assertFalse(ok)

    async def test_concept_consistency_drops_when_one_language_diverges(self):
        async def es_diverges(text):
            c = BY_TEXT[text]
            wrong = INTENTS[(AXIS[c.intent] + 1) % len(INTENTS)]
            return f"<{wrong if (c.language == 'es' and c.concept_id == 'price_how_much') else c.intent}>"

        r = await evaluate(CASES, POINTS, es_diverges, embed)
        # 10 intents (concepts), 1 concept broken by es_diverges -> 9/10
        n_concepts = len({c.concept_id for c in CASES})
        self.assertAlmostEqual(r.concept_consistency, (n_concepts - 1) / n_concepts)

    async def test_consistent_but_wrong_is_visible_as_low_accuracy(self):
        """Cross-language agreement alone is not success: everyone can
        agree on the wrong intent."""
        async def all_wrong(text):
            c = BY_TEXT[text]
            return f"<{INTENTS[(AXIS[c.intent] + 1) % len(INTENTS)]}>"

        r = await evaluate(CASES, POINTS, all_wrong, embed)
        self.assertEqual(r.concept_consistency, 1.0)
        self.assertEqual(r.overall.accuracy, 0.0)
        self.assertFalse(passes_gate(r)[0])

    async def test_report_formats(self):
        r = await evaluate(CASES, POINTS, perfect_normalize, embed)
        out = format_report(r)
        self.assertIn("by language:", out)
        self.assertIn("transliterated", out)


if __name__ == "__main__":
    unittest.main()


# ── calibration support (suggest_thresholds / report_to_dict) ───────
import json as _json
import unittest as _unittest

from app.language.generalization_eval import (
    MIN_CASES_FLOOR,
    MIN_SCORED_FOR_SUGGESTION,
    SUGGESTION_MARGIN,
    GeneralizationReport,
    Slice,
    report_to_dict,
    suggest_thresholds,
)


def _report(n_per_lang, correct_per_lang, concept=1.0, langs=("en", "bn", "hi")):
    r = GeneralizationReport()
    for lang in langs:
        r.by_language[lang] = Slice(n_per_lang, correct_per_lang)
        r.overall.n += n_per_lang
        r.overall.correct += correct_per_lang
    r.concept_consistency = concept
    r.concepts_scored = 10
    return r


class TestSuggestThresholds(_unittest.TestCase):
    def test_refuses_empty_report(self):
        out = suggest_thresholds(GeneralizationReport())
        self.assertFalse(out["ok"])

    def test_refuses_small_sample(self):
        out = suggest_thresholds(_report(5, 5))  # 15 cases
        self.assertFalse(out["ok"])
        self.assertIn(str(MIN_SCORED_FOR_SUGGESTION), out["reason"])

    def test_margin_below_observed(self):
        out = suggest_thresholds(_report(40, 36, concept=0.9))  # 90%
        self.assertTrue(out["ok"])
        s = out["suggested"]
        self.assertAlmostEqual(s["min_overall"], round(0.9 - SUGGESTION_MARGIN, 2))
        self.assertAlmostEqual(s["min_per_language"], round(0.9 - SUGGESTION_MARGIN, 2))
        self.assertAlmostEqual(s["min_concept_consistency"], 0.85)

    def test_weakest_language_sets_floor(self):
        r = _report(40, 36)
        r.by_language["th"] = Slice(40, 20)  # 50%
        r.overall.n += 40
        r.overall.correct += 20
        out = suggest_thresholds(r)
        self.assertAlmostEqual(out["suggested"]["min_per_language"], 0.45)
        self.assertAlmostEqual(out["observed"]["weakest_language"], 0.5)

    def test_coverage_never_below_floor_and_capped(self):
        out = suggest_thresholds(_report(40, 36))
        c = out["suggested"]["min_cases_per_language"]
        self.assertGreaterEqual(c, MIN_CASES_FLOOR)
        self.assertLessEqual(c, MIN_CASES_FLOOR * 2)

    def test_warns_when_brain_weaker_than_placeholder(self):
        out = suggest_thresholds(_report(40, 24))  # 60%
        self.assertTrue(any("weaker than the placeholder" in w for w in out["warnings"]))

    def test_never_negative(self):
        out = suggest_thresholds(_report(40, 0, concept=0.0))
        self.assertGreaterEqual(out["suggested"]["min_overall"], 0.0)

    def test_report_to_dict_is_json_serializable(self):
        d = report_to_dict(_report(10, 8))
        _json.dumps(d)
        self.assertEqual(d["overall"]["n"], 30)
        self.assertEqual(d["by_language"]["en"]["correct"], 8)


class TestGateConstants(_unittest.TestCase):
    def test_docs_names_exist_and_defaults_match(self):
        from app.language import generalization_eval as g
        self.assertEqual(g.GateThresholds().min_per_language, g.MIN_LANG_ACCURACY)
        self.assertEqual(g.GateThresholds().min_cases_per_language, g.MIN_COVERAGE)

    def test_still_flagged_uncalibrated(self):
        from app.language import generalization_eval as g
        self.assertFalse(g.THRESHOLDS_CALIBRATED)
