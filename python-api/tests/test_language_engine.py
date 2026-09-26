"""
Tests for the Language Engine v2 increment (structured semantic
understanding: communication_style, is_ambiguous/ambiguity_reason,
entity_spans, original_message).

Runs the REAL app.language.language_engine / app.intent.intent_engine
code. Only ai_service.complete_json (the actual network boundary to
Claude) is mocked -- everything else (parsing, defaults, dataclass
construction, prompt assembly) is exercised for real.

Run inside the python-api container where real dependencies are
installed:
    docker compose exec python-api python -m unittest tests.test_language_engine -v

(This file also runs standalone with only `anthropic`/`pydantic_settings`
stubbed -- see the project's test notes for how the stubs work.)
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.intent.intent_engine import intent_engine
from app.language.language_engine import language_engine
from app.language.language_types import LanguageResult


def mock_llm_json(return_value):
    """Patches ai_service.complete_json (used by both engines) to
    return a fixed dict, as if the LLM had replied with that JSON."""
    return patch(
        "app.language.language_engine.ai_service.complete_json",
        new=AsyncMock(return_value=return_value),
    )


def mock_intent_llm_json(return_value):
    return patch(
        "app.intent.intent_engine.ai_service.complete_json",
        new=AsyncMock(return_value=return_value),
    )


class TestLanguageEngineBangla(unittest.IsolatedAsyncioTestCase):
    async def test_pure_bangla_message(self):
        message = "আমার অর্ডারটা কবে পাবো?"
        with mock_llm_json(
            {
                "detectedLanguage": "bn",
                "replyLanguage": "bn",
                "normalizedMessage": "When will I receive my order?",
                "confidence": 0.95,
                "communicationStyle": "neutral",
                "isAmbiguous": False,
                "ambiguityReason": None,
                "entitySpans": [],
            }
        ):
            result = await language_engine.understand(message)

        self.assertIsInstance(result, LanguageResult)
        self.assertEqual(result.original_message, message)  # preserved verbatim
        self.assertEqual(result.detected_language, "bn")
        self.assertEqual(result.reply_language, "bn")
        self.assertEqual(result.normalized_message, "When will I receive my order?")
        self.assertFalse(result.is_ambiguous)
        self.assertEqual(result.entity_spans, [])
    async def test_usage_out_is_forwarded_to_ai_service(self):
        message = "আমার অর্ডারটা কবে পাবো?"
        usage_out = {}

        with mock_llm_json(
            {
                "detectedLanguage": "bn",
                "replyLanguage": "bn",
                "normalizedMessage": "When will I receive my order?",
                "confidence": 0.95,
                "communicationStyle": "neutral",
                "isAmbiguous": False,
                "ambiguityReason": None,
                "entitySpans": [],
            }
        ) as mocked_complete_json:
            await language_engine.understand(
                message,
                usage_out=usage_out,
            )

        mocked_complete_json.assert_awaited_once()
        self.assertIs(
            mocked_complete_json.await_args.kwargs["usage_out"],
            usage_out,
        )


class TestLanguageEngineBanglish(unittest.IsolatedAsyncioTestCase):
    async def test_banglish_message(self):
        message = "vai amar order ta kobe pabo? SKU-AX4921"
        with mock_llm_json(
            {
                "detectedLanguage": "mixed",  # banglish is romanized bangla; LLM may tag as "mixed" or "bn"
                "replyLanguage": "bn",
                "normalizedMessage": "When will I get my order? SKU-AX4921",
                "confidence": 0.88,
                "communicationStyle": "informal",
                "isAmbiguous": False,
                "ambiguityReason": None,
                "entitySpans": ["SKU-AX4921"],
            }
        ):
            result = await language_engine.understand(message)

        self.assertEqual(result.original_message, message)
        self.assertEqual(result.communication_style, "informal")
        self.assertIn("SKU-AX4921", result.entity_spans)
        self.assertIn("SKU-AX4921", result.normalized_message)  # codes preserved, not translated


class TestLanguageEngineMixed(unittest.IsolatedAsyncioTestCase):
    async def test_bangla_english_mixed_message(self):
        message = "আমার Nike Air Max size 42 লাগবে, দাম কত?"
        with mock_llm_json(
            {
                "detectedLanguage": "mixed",
                "replyLanguage": "bn",
                "normalizedMessage": "I need Nike Air Max size 42, what is the price?",
                "confidence": 0.92,
                "communicationStyle": "neutral",
                "isAmbiguous": False,
                "ambiguityReason": None,
                "entitySpans": ["Nike Air Max", "42"],
            }
        ):
            result = await language_engine.understand(message)

        self.assertEqual(result.detected_language, "mixed")
        self.assertEqual(result.entity_spans, ["Nike Air Max", "42"])
        # Original, untranslated product name preserved in the original message
        self.assertIn("Nike Air Max", result.original_message)


class TestLanguageEngineContextualFollowUp(unittest.IsolatedAsyncioTestCase):
    async def test_short_followup_flagged_ambiguous(self):
        """A short follow-up with a pronoun and no antecedent WITHIN this
        single message should be flagged ambiguous -- the Language Engine
        has no conversation history access, so it must not silently guess."""
        message = "oita koto?"
        with mock_llm_json(
            {
                "detectedLanguage": "en",
                "replyLanguage": "bn",
                "normalizedMessage": "How much is that?",
                "confidence": 0.6,
                "communicationStyle": "informal",
                "isAmbiguous": True,
                "ambiguityReason": "Refers to 'that' with no antecedent in this message.",
                "entitySpans": [],
            }
        ):
            result = await language_engine.understand(message)

        self.assertTrue(result.is_ambiguous)
        self.assertIsNotNone(result.ambiguity_reason)
        self.assertEqual(result.original_message, message)


class TestLanguageEngineAmbiguousInput(unittest.IsolatedAsyncioTestCase):
    async def test_vague_incomplete_message(self):
        message = "eta thik na"
        with mock_llm_json(
            {
                "detectedLanguage": "mixed",
                "replyLanguage": "bn",
                "normalizedMessage": "This is not right.",
                "confidence": 0.4,
                "communicationStyle": "informal",
                "isAmbiguous": True,
                "ambiguityReason": "Unclear what 'this' refers to.",
                "entitySpans": [],
            }
        ):
            result = await language_engine.understand(message)

        self.assertTrue(result.is_ambiguous)
        self.assertLess(result.confidence, 0.6)


class TestOriginalMessagePreservation(unittest.IsolatedAsyncioTestCase):
    async def test_original_message_never_overwritten_even_with_missing_fields(self):
        """Even a minimal/degenerate LLM response must never cause
        original_message to be replaced by the normalized form."""
        message = "amar order ta kothay?"
        with mock_llm_json({}):  # LLM returned nothing usable
            result = await language_engine.understand(message)

        self.assertEqual(result.original_message, message)
        # normalized_message falls back to the original when the LLM
        # gives nothing -- but original_message is a SEPARATE field and
        # is set from the raw `message` argument directly, not derived
        # from normalized_message, so this holds even in the fallback case.
        self.assertEqual(result.normalized_message, message)
        self.assertEqual(result.detected_language, "other")
        self.assertEqual(result.communication_style, "neutral")
        self.assertFalse(result.is_ambiguous)
        self.assertEqual(result.entity_spans, [])

    async def test_original_message_unchanged_across_many_calls(self):
        messages = [
            "আপনার প্রোডাক্ট কি এখনো পাওয়া যাচ্ছে?",
            "is this still in stock??",
            "bhai deliver kobe hobe 🙏",
        ]
        for message in messages:
            with mock_llm_json(
                {
                    "detectedLanguage": "other",
                    "replyLanguage": "en",
                    "normalizedMessage": "placeholder normalized text, unrelated to original",
                    "confidence": 0.7,
                    "communicationStyle": "neutral",
                    "isAmbiguous": False,
                    "ambiguityReason": None,
                    "entitySpans": [],
                }
            ):
                result = await language_engine.understand(message)
            self.assertEqual(
                result.original_message,
                message,
                "original_message must exactly equal what was passed in, "
                "regardless of what normalized_message became",
            )
            self.assertNotEqual(
                result.original_message,
                result.normalized_message,
                "this test's mock intentionally returns a different "
                "normalized_message -- confirms the two fields are "
                "genuinely independent, not aliases of each other",
            )


class TestDefensiveParsing(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_entity_spans_falls_back_to_empty_list(self):
        message = "test"
        with mock_llm_json(
            {
                "detectedLanguage": "en",
                "replyLanguage": "en",
                "normalizedMessage": "test",
                "confidence": 0.9,
                "entitySpans": "not-a-list",  # malformed
            }
        ):
            result = await language_engine.understand(message)
        self.assertEqual(result.entity_spans, [])

    async def test_entity_spans_filters_non_string_items(self):
        message = "test"
        with mock_llm_json(
            {
                "detectedLanguage": "en",
                "replyLanguage": "en",
                "normalizedMessage": "test",
                "confidence": 0.9,
                "entitySpans": ["valid", 123, None, "", "  ", "also valid"],
            }
        ):
            result = await language_engine.understand(message)
        self.assertEqual(result.entity_spans, ["valid", "also valid"])

    async def test_missing_optional_fields_use_safe_defaults(self):
        message = "test"
        with mock_llm_json(
            {
                "detectedLanguage": "en",
                "replyLanguage": "en",
                "normalizedMessage": "test",
                "confidence": 0.9,
                # communicationStyle, isAmbiguous, ambiguityReason, entitySpans all omitted
            }
        ):
            result = await language_engine.understand(message)
        self.assertEqual(result.communication_style, "neutral")
        self.assertFalse(result.is_ambiguous)
        self.assertIsNone(result.ambiguity_reason)
        self.assertEqual(result.entity_spans, [])


class TestIntentEngineEntityHints(unittest.IsolatedAsyncioTestCase):
    async def test_entity_hints_are_injected_into_prompt(self):
        captured_prompt = {}

        async def fake_complete_json(prompt, system=None):
            captured_prompt["value"] = prompt
            return {"intent": "PRICE_INQUIRY", "confidence": 0.9, "entities": {}}

        with patch(
            "app.intent.intent_engine.ai_service.complete_json",
            new=AsyncMock(side_effect=fake_complete_json),
        ):
            await intent_engine.classify(
                "what is the price of Nike Air Max size 42?",
                entity_hints=["Nike Air Max", "42"],
            )

        self.assertIn("Nike Air Max", captured_prompt["value"])
        self.assertIn("unverified, untyped", captured_prompt["value"])

    async def test_no_hints_produces_no_hint_block_backward_compatible(self):
        captured_prompt = {}

        async def fake_complete_json(prompt, system=None):
            captured_prompt["value"] = prompt
            return {"intent": "GENERAL_QUESTION", "confidence": 0.9, "entities": {}}

        with patch(
            "app.intent.intent_engine.ai_service.complete_json",
            new=AsyncMock(side_effect=fake_complete_json),
        ):
            # exactly the old call signature -- no entity_hints argument at all
            await intent_engine.classify("hello")

        self.assertNotIn("unverified, untyped", captured_prompt["value"])

    async def test_empty_hints_list_also_produces_no_hint_block(self):
        captured_prompt = {}

        async def fake_complete_json(prompt, system=None):
            captured_prompt["value"] = prompt
            return {"intent": "GENERAL_QUESTION", "confidence": 0.9, "entities": {}}

        with patch(
            "app.intent.intent_engine.ai_service.complete_json",
            new=AsyncMock(side_effect=fake_complete_json),
        ):
            await intent_engine.classify("hello", entity_hints=[])

        self.assertNotIn("unverified, untyped", captured_prompt["value"])

    async def test_intent_engine_still_owns_entity_typing(self):
        """Hints are advisory only -- the returned `entities` dict comes
        entirely from the Intent Engine's own LLM call, never copied
        directly from entity_hints."""
        with mock_intent_llm_json(
            {
                "intent": "PRICE_INQUIRY",
                "confidence": 0.9,
                "entities": {"product": "Nike Air Max", "size": "42"},
            }
        ):
            result = await intent_engine.classify(
                "price of Nike Air Max 42?", entity_hints=["Nike Air Max", "42", "junk-span"]
            )

        # entities dict is exactly what the (mocked) LLM returned -- the
        # hint "junk-span" does not leak into it just because it was a hint
        self.assertEqual(result.entities, {"product": "Nike Air Max", "size": "42"})
        self.assertNotIn("junk-span", result.entities.values())
    async def test_usage_out_is_forwarded_to_ai_service(self):
        usage_out = {}

        with mock_intent_llm_json(
            {
                "intent": "PRICE_INQUIRY",
                "confidence": 0.9,
                "entities": {},
            }
        ) as mocked_complete_json:
            await intent_engine.classify(
                "price of Nike Air Max 42?",
                usage_out=usage_out,
            )

        mocked_complete_json.assert_awaited_once()
        self.assertIs(
            mocked_complete_json.await_args.kwargs["usage_out"],
            usage_out,
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
