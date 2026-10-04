"""Tests for app/language/glb_orchestrator.py (Phase 5 orchestration, advisory).
    docker compose exec python-api python -m unittest tests.test_glb_orchestrator -v
"""
import unittest
from dataclasses import fields

from app.language import glb
from app.language.glb_orchestrator import TurnPlan, plan_to_dict, plan_turn


def U(**kw):
    base = dict(intent="PRICE_INQUIRY", intent_confidence=0.95, intent_source="llm", language="bn", script="bengali")
    base.update(kw)
    return glb.assemble_turn_understanding(**base)


class TestPlan(unittest.TestCase):
    def test_clean_turn_own_model_ok_and_automation_allowed(self):
        p = plan_turn(U())
        self.assertEqual(p.understanding_source, "own_model_ok")
        self.assertEqual(p.automation, "allowed")
        self.assertFalse(p.clarify_before_acting)
        self.assertEqual(p.reasons, ())

    def test_section3_worked_example_learnable_but_not_automatable(self):
        p = plan_turn(U(intent="ORDER_STATUS"))
        self.assertEqual(p.automation, "human_review_required")
        self.assertTrue(p.record_as_learning_material)   # language is learnable
        self.assertEqual(p.understanding_source, "own_model_ok")  # understanding is fine

    def test_ambiguous_clarifies_and_keeps_teacher(self):
        p = plan_turn(U(is_ambiguous=True))
        self.assertTrue(p.clarify_before_acting)
        self.assertEqual(p.understanding_source, "llm_teacher_required")

    def test_novel_flags_triage_and_teacher(self):
        p = plan_turn(U(is_novel=True))
        self.assertTrue(p.flag_for_triage)
        self.assertEqual(p.understanding_source, "llm_teacher_required")

    def test_low_confidence_keeps_teacher(self):
        p = plan_turn(U(intent_confidence=0.2))
        self.assertEqual(p.understanding_source, "llm_teacher_required")

    def test_no_intent_keeps_teacher(self):
        self.assertEqual(plan_turn(U(intent=None)).understanding_source, "llm_teacher_required")

    def test_code_mixed_or_transliterated_alone_do_not_force_teacher(self):
        p = plan_turn(U(is_transliterated=True, code_mixing="bn_en"))
        self.assertEqual(p.understanding_source, "own_model_ok")

    def test_reply_language_passed_through(self):
        self.assertEqual(plan_turn(U(reply_language="bn")).reply_language, "bn")

    def test_accepts_dict_form_used_by_core_agent(self):
        d = glb.understanding_to_dict(U(is_novel=True))
        self.assertTrue(plan_turn(d).flag_for_triage)


class TestSafety(unittest.TestCase):
    def test_advisory_and_never_enacted(self):
        for u in (U(), U(is_novel=True), U(intent="CREATE_ORDER")):
            p = plan_turn(u)
            self.assertTrue(p.advisory)
            self.assertFalse(p.enacted)

    def test_garbage_input_degrades_to_safe_plan_not_exception(self):
        for bad in (None, 42, "x", {"phenomena": 5}, object()):
            p = plan_turn(bad)
            self.assertEqual(p.automation, "human_review_required")
            self.assertEqual(p.understanding_source, "llm_teacher_required")

    def test_empty_dict_is_safe_plan(self):
        p = plan_turn({})
        self.assertEqual(p.automation, "human_review_required")

    def test_no_person_inference_fields(self):
        names = " ".join(f.name for f in fields(TurnPlan))
        for w in glb.FORBIDDEN_FIELD_WORDS:
            self.assertNotIn(w, names)

    def test_dict_form_is_json_safe(self):
        import json
        json.dumps(plan_to_dict(plan_turn(U(is_ambiguous=True))))

    def test_language_never_changes_automation(self):
        a = plan_turn(U(language="bn")).automation
        b = plan_turn(U(language="ja", script="kana")).automation
        self.assertEqual(a, b)

    def test_core_agent_wires_plan_additively_and_isolated(self):
        from pathlib import Path
        src = (Path(__file__).parents[1] / "app/agent/core_agent.py").read_text()
        self.assertIn('"plan": plan', src)
        self.assertIn("glb plan failed (ignored)", src)


if __name__ == "__main__":
    unittest.main()
