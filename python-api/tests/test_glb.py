"""
Tests for app/language/glb.py (Phase 5 capability-A umbrella).
Pure -- no DB, no LLM. Run:
    docker compose exec python-api python -m unittest tests.test_glb -v
"""

import json
import unittest

from app.language import glb
from app.language.glb import (
    CAPABILITIES, CapabilityStatus, FORBIDDEN_FIELD_WORDS, LOW_CONFIDENCE,
    assemble_turn_understanding, understanding_to_dict,
)


class TestRegistry(unittest.TestCase):
    def test_all_thirteen_capabilities_a_to_m(self):
        self.assertEqual([c.code for c in CAPABILITIES], list("ABCDEFGHIJKLM"))

    def test_every_listed_module_exists(self):
        self.assertEqual(glb.missing_modules(), [])

    def test_missing_module_is_detected(self):
        from pathlib import Path
        self.assertTrue(glb.missing_modules(Path("/nonexistent")))

    def test_tool_engine_is_not_claimed_as_built(self):
        self.assertEqual(glb.capability("K").status, CapabilityStatus.NOT_BUILT)

    def test_umbrella_is_not_overclaimed(self):
        self.assertEqual(glb.capability("A").status, CapabilityStatus.PARTIAL)

    def test_unknown_code_raises(self):
        with self.assertRaises(KeyError):
            glb.capability("Z")

    def test_report_lists_every_code(self):
        rep = glb.capability_report()
        for c in "ABCDEFGHIJKLM":
            self.assertIn(f"  {c}  ", rep)


class TestAssemble(unittest.TestCase):
    def test_defaults_are_honest_not_guesses(self):
        u = assemble_turn_understanding()
        self.assertEqual((u.language, u.script), ("und", "und"))
        self.assertIsNone(u.intent)
        self.assertEqual(u.phenomena, ())

    def test_blank_strings_become_und(self):
        u = assemble_turn_understanding(language="  ", script="")
        self.assertEqual((u.language, u.script), ("und", "und"))

    def test_phenomena_flags(self):
        u = assemble_turn_understanding(
            language="bn,en", code_mixing="intra_sentential", is_transliterated=True,
            is_ambiguous=True, is_novel=True, intent_confidence=LOW_CONFIDENCE - 0.01)
        self.assertEqual(u.phenomena, ("code_mixed", "transliterated", "ambiguous", "novel", "low_confidence"))

    def test_confident_clean_turn_has_no_phenomena(self):
        u = assemble_turn_understanding(language="en", script="latin", intent="PRICE_INQUIRY", intent_confidence=0.95)
        self.assertEqual(u.phenomena, ())

    def test_bad_confidence_type_never_raises(self):
        u = assemble_turn_understanding(intent_confidence="high")  # type: ignore[arg-type]
        self.assertNotIn("low_confidence", u.phenomena)

    def test_order_status_understood_but_not_automatable(self):
        # §3 worked example: learn the phrasing, never auto-act.
        u = assemble_turn_understanding(language="bn", is_transliterated=True, intent="ORDER_STATUS")
        self.assertTrue(u.language_learning_eligible)
        self.assertTrue(u.promotion_eligible)
        self.assertTrue(u.training_eligible)
        self.assertFalse(u.automation_eligible)

    def test_ordinary_intent_is_automatable(self):
        self.assertTrue(assemble_turn_understanding(intent="PRICE_INQUIRY").automation_eligible)

    def test_record_is_frozen(self):
        u = assemble_turn_understanding(intent="X")
        with self.assertRaises(Exception):
            u.intent = "Y"  # type: ignore[misc]

    def test_dict_is_json_serializable(self):
        d = understanding_to_dict(assemble_turn_understanding(language="hi", is_transliterated=True))
        json.dumps(d)
        self.assertEqual(d["phenomena"], ["transliterated"])


class TestSection53Boundary(unittest.TestCase):
    def test_no_person_inference_fields(self):
        for name in glb.field_names():
            for bad in FORBIDDEN_FIELD_WORDS:
                self.assertNotIn(bad, name.lower(), f"{name} looks like a person-inference field (§5.3)")


if __name__ == "__main__":
    unittest.main()
