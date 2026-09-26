import unittest
from unittest.mock import AsyncMock, patch

from app.kernel.kernel import kernel


class TestKernelTokenUsage(unittest.IsolatedAsyncioTestCase):
    async def test_token_usage_is_accumulated_across_ai_calls(self):
        language_usage = {}
        intent_usage = {}
        reply_usage = {}
        memory_usage = {}

        async def fake_understand(message, *, usage_out=None):
            usage_out.update(
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                }
            )
            return type(
                "LanguageResult",
                (),
                {
                    "normalized_message": message,
                    "entity_spans": [],
                    "detected_language": "bn",
                    "reply_language": "bn",
                    "confidence": 0.95,
                    "communication_style": "neutral",
                    "is_ambiguous": False,
                    "ambiguity_reason": None,
                },
            )()

        async def fake_classify(
            message,
            entity_hints=None,
            *,
            usage_out=None,
        ):
            usage_out.update(
                {
                    "input_tokens": 200,
                    "output_tokens": 30,
                }
            )
            return type(
                "IntentResult",
                (),
                {
                    "intent": type(
                        "Intent",
                        (),
                        {
                            "value": "GENERAL_INQUIRY",
                        },
                    )(),
                    "confidence": 0.95,
                },
            )()

        async def fake_complete(
            prompt,
            system=None,
            *,
            usage_out=None,
        ):
            usage_out.update(
                {
                    "input_tokens": 300,
                    "output_tokens": 40,
                }
            )
            return "Test reply"

        async def fake_memory(
            db,
            tenant_id,
            customer_id,
            message,
            *,
            usage_out=None,
        ):
            usage_out.update(
                {
                    "input_tokens": 50,
                    "output_tokens": 10,
                }
            )
            return []

        fake_state = {
            "conversationState": "active",
        }

        with (
            patch(
                "app.kernel.kernel.language_engine.understand",
                new=AsyncMock(side_effect=fake_understand),
            ),
            patch(
                "app.kernel.kernel.intent_engine.classify",
                new=AsyncMock(side_effect=fake_classify),
            ),
            patch(
                "app.kernel.kernel.state_engine.update_state",
                new=AsyncMock(return_value=fake_state),
            ),
            patch(
                "app.kernel.kernel.ai_service.complete",
                new=AsyncMock(side_effect=fake_complete),
            ),
            patch(
                "app.kernel.kernel.memory_service.extract_and_store",
                new=AsyncMock(side_effect=fake_memory),
            ),
        ):
            response = await kernel.run(
                db=AsyncMock(),
                tenant_id=1,
                customer_id=2,
                conversation_id=3,
                message="আমার অর্ডারের অবস্থা কী?",
            )

        self.assertEqual(response.inputTokens, 650)
        self.assertEqual(response.outputTokens, 100)
        self.assertEqual(response.reply, "Test reply")


if __name__ == "__main__":
    unittest.main(verbosity=2)
