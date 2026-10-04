"""
Tests for token-usage persistence (db/init/009_token_usage.sql).

Two things are tested here, matching the two places this feature
actually lives:

1. AIService.complete()/complete_json() — does `usage_out` get filled
   correctly, does it stay backward-compatible for callers that don't
   pass it, does a missing/failed usage report degrade safely?
   Mocks only the Anthropic client's .messages.create() call -- the
   rest of AIService runs for real.

2. _accumulate_token_usage() (app/kernel/kernel.py) — the pure
   aggregation function extracted specifically so this could be tested
   without needing Kernel.run()'s full dependency graph. Covers the
   exact scenarios requested: multiple calls summing correctly, a
   valid zero being distinguished from "unknown", and a later None
   never overwriting an earlier known value.

Trace-level persistence (does input_tokens/output_tokens actually
reach the SQL INSERT) is covered separately in
test_agent_foundation.py, alongside the other trace regression tests
for the same reason -- consistent with how this codebase organizes
tests by "which existing test file already covers this data flow"
rather than duplicating a parallel trace-test file per feature.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.ai.ai_service import ai_service
from app.kernel.kernel import _accumulate_token_usage


class FakeContentBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeAnthropicResponse:
    def __init__(self, text="ok", usage=None):
        self.content = [FakeContentBlock(text)]
        self.usage = usage


def mock_anthropic_call(response):
    return patch.object(
        ai_service._client.messages, "create", new=AsyncMock(return_value=response)
    )


class TestAIServiceUsageOut(unittest.IsolatedAsyncioTestCase):
    async def test_complete_fills_usage_out_when_provided(self):
        response = FakeAnthropicResponse(usage=FakeUsage(input_tokens=42, output_tokens=17))
        usage_out: dict = {}
        with mock_anthropic_call(response):
            reply = await ai_service.complete("hello", usage_out=usage_out)
        self.assertEqual(reply, "ok")
        self.assertEqual(usage_out["input_tokens"], 42)
        self.assertEqual(usage_out["output_tokens"], 17)

    async def test_complete_return_type_unchanged_when_usage_out_omitted(self):
        """The whole point of usage_out being optional: every existing
        caller that doesn't know about it keeps getting a plain str."""
        response = FakeAnthropicResponse(text="plain reply", usage=FakeUsage(1, 1))
        with mock_anthropic_call(response):
            reply = await ai_service.complete("hello")
        self.assertIsInstance(reply, str)
        self.assertEqual(reply, "plain reply")

    async def test_complete_json_still_returns_dict_and_also_fills_usage_out(self):
        response = FakeAnthropicResponse(
            text='{"intent": "PRICE_INQUIRY"}', usage=FakeUsage(input_tokens=30, output_tokens=8)
        )
        usage_out: dict = {}
        with mock_anthropic_call(response):
            result = await ai_service.complete_json("prompt", usage_out=usage_out)
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"intent": "PRICE_INQUIRY"})
        self.assertEqual(usage_out["input_tokens"], 30)
        self.assertEqual(usage_out["output_tokens"], 8)

    async def test_complete_json_return_type_unchanged_when_usage_out_omitted(self):
        response = FakeAnthropicResponse(text='{"ok": true}', usage=FakeUsage(1, 1))
        with mock_anthropic_call(response):
            result = await ai_service.complete_json("prompt")
        self.assertIsInstance(result, dict)

    async def test_missing_usage_on_response_leaves_usage_out_with_none_values(self):
        """Anthropic's SDK might not attach `.usage` at all (defensive
        getattr already existed before this phase) -- usage_out must
        reflect "unknown", not silently stay an empty dict or crash."""
        response = FakeAnthropicResponse(usage=None)
        usage_out: dict = {}
        with mock_anthropic_call(response):
            await ai_service.complete("hello", usage_out=usage_out)
        self.assertIsNone(usage_out["input_tokens"])
        self.assertIsNone(usage_out["output_tokens"])

    async def test_valid_zero_token_usage_is_preserved_not_dropped(self):
        response = FakeAnthropicResponse(usage=FakeUsage(input_tokens=0, output_tokens=0))
        usage_out: dict = {}
        with mock_anthropic_call(response):
            await ai_service.complete("hello", usage_out=usage_out)
        self.assertEqual(usage_out["input_tokens"], 0)
        self.assertEqual(usage_out["output_tokens"], 0)

    async def test_failed_llm_call_never_populates_usage_out(self):
        with patch.object(
            ai_service._client.messages, "create", new=AsyncMock(side_effect=RuntimeError("api down"))
        ):
            usage_out: dict = {}
            with self.assertRaises(RuntimeError):
                await ai_service.complete("hello", usage_out=usage_out)
            # The exception propagates before usage_out is ever touched --
            # callers must treat an empty dict the same as "no usage".
            self.assertEqual(usage_out, {})


class TestAccumulateTokenUsage(unittest.TestCase):
    """Pure function -- app/kernel/kernel.py's _accumulate_token_usage.
    Covers exactly the scenarios called out in this phase's approval:
    multiple LLM calls, and valid zero values."""

    def test_single_call_sets_totals(self):
        totals = {"input_tokens": None, "output_tokens": None}
        _accumulate_token_usage(totals, {"input_tokens": 100, "output_tokens": 40})
        self.assertEqual(totals, {"input_tokens": 100, "output_tokens": 40})

    def test_multiple_calls_sum_correctly(self):
        """Simulates one agent run's four LLM calls (language, intent,
        reply, memory extraction)."""
        totals = {"input_tokens": None, "output_tokens": None}
        _accumulate_token_usage(totals, {"input_tokens": 50, "output_tokens": 10})   # language
        _accumulate_token_usage(totals, {"input_tokens": 80, "output_tokens": 15})   # intent
        _accumulate_token_usage(totals, {"input_tokens": 200, "output_tokens": 60})  # reply
        _accumulate_token_usage(totals, {"input_tokens": 40, "output_tokens": 12})   # memory
        self.assertEqual(totals["input_tokens"], 50 + 80 + 200 + 40)
        self.assertEqual(totals["output_tokens"], 10 + 15 + 60 + 12)

    def test_a_none_contribution_does_not_overwrite_earlier_known_usage(self):
        totals = {"input_tokens": None, "output_tokens": None}
        _accumulate_token_usage(totals, {"input_tokens": 100, "output_tokens": 30})
        _accumulate_token_usage(totals, None)  # e.g. a call whose usage was never captured
        _accumulate_token_usage(totals, {"input_tokens": 50, "output_tokens": 20})
        self.assertEqual(totals["input_tokens"], 150)
        self.assertEqual(totals["output_tokens"], 50)

    def test_empty_dict_contribution_is_also_a_noop(self):
        totals = {"input_tokens": None, "output_tokens": None}
        _accumulate_token_usage(totals, {"input_tokens": 10, "output_tokens": 5})
        _accumulate_token_usage(totals, {})
        self.assertEqual(totals, {"input_tokens": 10, "output_tokens": 5})

    def test_no_calls_ever_report_usage_totals_stay_none(self):
        totals = {"input_tokens": None, "output_tokens": None}
        _accumulate_token_usage(totals, None)
        _accumulate_token_usage(totals, {})
        _accumulate_token_usage(totals, {"input_tokens": None, "output_tokens": None})
        self.assertIsNone(totals["input_tokens"])
        self.assertIsNone(totals["output_tokens"])

    def test_valid_zero_is_distinguished_from_unknown(self):
        """The exact case called out in the approval: a call reporting
        a real 0 must flip totals from None ('unknown') to 0 ('known,
        zero') -- not be treated as 'nothing happened'."""
        totals = {"input_tokens": None, "output_tokens": None}
        _accumulate_token_usage(totals, {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(totals["input_tokens"], 0)
        self.assertEqual(totals["output_tokens"], 0)
        self.assertIsNotNone(totals["input_tokens"])  # explicit: 0 is not None

    def test_zero_then_a_real_value_sums_correctly(self):
        totals = {"input_tokens": None, "output_tokens": None}
        _accumulate_token_usage(totals, {"input_tokens": 0, "output_tokens": 0})
        _accumulate_token_usage(totals, {"input_tokens": 50, "output_tokens": 20})
        self.assertEqual(totals["input_tokens"], 50)
        self.assertEqual(totals["output_tokens"], 20)

    def test_partial_usage_only_one_key_present(self):
        totals = {"input_tokens": None, "output_tokens": None}
        _accumulate_token_usage(totals, {"input_tokens": 25})  # output_tokens absent entirely
        self.assertEqual(totals["input_tokens"], 25)
        self.assertIsNone(totals["output_tokens"])

    def test_mutates_totals_in_place_and_returns_none(self):
        totals = {"input_tokens": None, "output_tokens": None}
        result = _accumulate_token_usage(totals, {"input_tokens": 5, "output_tokens": 5})
        self.assertIsNone(result)
        self.assertEqual(totals["input_tokens"], 5)  # mutated in place, not returned


if __name__ == "__main__":
    unittest.main(verbosity=2)
