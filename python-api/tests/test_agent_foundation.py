"""
Tests for the architectural-foundation modules added for:
Tenant Brain, Capability Registry, Goal/Outcome, and Agent Run Trace.

Identity Resolution is tested separately in node-api (TypeScript) --
see node-api/src/modules/customers/identity_resolution.test.ts.

Capability Registry and Goal/Outcome have zero external dependencies
(no DB, no LLM) and are tested directly. Tenant Brain and the trace
write path use a fake in-memory "session" object satisfying just the
`execute()`/`commit()` shape they need, rather than a real database --
these are unit tests of the Python logic, not integration tests
against Postgres.
"""

import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock

from app.capabilities.capability_registry import CapabilityRegistry
from app.capabilities.capability_types import CapabilityDefinition
from app.goal.goal_types import GoalSignal, infer_goal_signal
from app.intent.intent_types import IntentType


class TestCapabilityRegistry(unittest.TestCase):
    def test_register_and_get(self):
        registry = CapabilityRegistry()
        cap = CapabilityDefinition(
            name="check_product_stock",
            description="Check whether a product is in stock",
            input_schema={"product_id": "string"},
        )
        registry.register(cap)

        self.assertIs(registry.get("check_product_stock"), cap)
        self.assertIsNone(registry.get("does_not_exist"))

    def test_registering_duplicate_name_raises(self):
        registry = CapabilityRegistry()
        cap = CapabilityDefinition(name="dup", description="first")
        registry.register(cap)
        with self.assertRaises(ValueError):
            registry.register(CapabilityDefinition(name="dup", description="second"))

    def test_starts_empty(self):
        # A fresh registry (like the module-level singleton) must start
        # with zero capabilities -- no real tool existed before this
        # task, so nothing should be pre-registered.
        registry = CapabilityRegistry()
        self.assertEqual(registry.list_all(), [])

    def test_list_enabled_filters_disabled_capabilities(self):
        registry = CapabilityRegistry()
        registry.register(CapabilityDefinition(name="a", description="", enabled=True))
        registry.register(CapabilityDefinition(name="b", description="", enabled=False))

        enabled_names = {c.name for c in registry.list_enabled()}
        self.assertEqual(enabled_names, {"a"})

    def test_default_capability_is_disabled_and_requires_confirmation(self):
        # Safety-by-default: declaring a capability's contract must not
        # accidentally make it runnable or unattended.
        cap = CapabilityDefinition(name="create_order", description="Create an order")
        self.assertFalse(cap.enabled)
        self.assertTrue(cap.requires_confirmation)
        self.assertIsNone(cap.handler)


class TestGoalSignal(unittest.TestCase):
    def test_known_intents_map_to_expected_goals(self):
        cases = {
            IntentType.CREATE_ORDER: "complete_purchase",
            IntentType.COMPLAINT: "resolve_complaint",
            IntentType.RETURN_REQUEST: "resolve_complaint",
            IntentType.PRICE_INQUIRY: "purchase_consideration",
            IntentType.GENERAL_QUESTION: "information_only",
        }
        for intent, expected_goal in cases.items():
            signal = infer_goal_signal(intent)
            self.assertIsInstance(signal, GoalSignal)
            self.assertEqual(signal.goal_type, expected_goal)
            self.assertEqual(signal.source, "intent_mapping")

    def test_is_a_pure_function_no_side_effects(self):
        # Calling it repeatedly with the same input must always give
        # the same output -- no hidden state, no I/O.
        results = [infer_goal_signal(IntentType.NEGOTIATION) for _ in range(5)]
        self.assertTrue(all(r == results[0] for r in results))


# ---------------------------------------------------------------------
# Fake async DB session -- just enough surface for tenant_brain.py and
# trace_service.py, which only ever call `await db.execute(text, params)`
# (returning an iterable of row-like objects) and `await db.commit()`.
# ---------------------------------------------------------------------


@dataclass
class FakeRow:
    vertical: str | None = None
    config: dict | None = None
    rule_key: str | None = None
    rule_value: dict | None = None

    def first(self):
        return self


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """Returns pre-programmed results in call order; records commits/executes."""

    def __init__(self, results):
        self._results = list(results)
        self.executed_queries = []
        self.commit_count = 0

    async def execute(self, query, params=None):
        self.executed_queries.append((query, params))
        return self._results.pop(0)

    async def commit(self):
        self.commit_count += 1


class TestTenantBrain(unittest.IsolatedAsyncioTestCase):
    async def test_loads_agent_config_and_business_rules_together(self):
        from app.agent.tenant_brain import load_tenant_brain

        agent_config_row = FakeRow(vertical="real_estate", config={"tools_enabled": True})
        rule_rows = [
            FakeRow(rule_key="max_discount_percent", rule_value={"value": 10}),
            FakeRow(rule_key="min_price", rule_value={"value": 4500}),
        ]
        session = FakeSession([FakeResult([agent_config_row]), FakeResult(rule_rows)])

        brain = await load_tenant_brain(session, tenant_id=7)

        self.assertEqual(brain.tenant_id, 7)
        self.assertEqual(brain.agent_config.vertical, "real_estate")
        self.assertTrue(brain.agent_config.tools_enabled)
        self.assertEqual(len(brain.business_rules), 2)
        self.assertEqual(brain.get_rule("max_discount_percent"), {"value": 10})
        self.assertIsNone(brain.get_rule("does_not_exist"))

    async def test_missing_agent_config_row_falls_back_to_defaults(self):
        from app.agent.tenant_brain import load_tenant_brain

        session = FakeSession([FakeResult([]), FakeResult([])])  # no agent_configs row, no rules
        brain = await load_tenant_brain(session, tenant_id=99)

        self.assertEqual(brain.agent_config.vertical, "ecommerce")  # default
        self.assertEqual(brain.business_rules, [])

    async def test_business_rules_query_failure_does_not_break_agent_config(self):
        from app.agent.tenant_brain import load_tenant_brain

        agent_config_row = FakeRow(vertical="construction", config={})

        class FailingSession(FakeSession):
            async def execute(self, query, params=None):
                self.executed_queries.append((query, params))
                if len(self.executed_queries) == 1:
                    return FakeResult([agent_config_row])
                raise RuntimeError("business_rules table temporarily unavailable")

        session = FailingSession([])
        brain = await load_tenant_brain(session, tenant_id=1)

        self.assertEqual(brain.agent_config.vertical, "construction")
        self.assertEqual(brain.business_rules, [], "a business_rules failure must not crash the whole load")


class TestAgentRunTrace(unittest.IsolatedAsyncioTestCase):
    async def test_record_trace_inserts_and_commits(self):
        from app.trace.trace_service import record_trace
        from app.trace.trace_types import AgentRunTrace
        import uuid

        session = FakeSession([None])  # INSERT doesn't need a meaningful return value
        trace = AgentRunTrace(
            agent_run_id=uuid.uuid4(),
            tenant_id=1,
            customer_id=2,
            conversation_id=3,
            message_id=4,
            intent="PRICE_INQUIRY",
            confidence=0.9,
            state={"product": "Nike Air Max"},
            decision="answered",
            response="It's 5000 taka.",
            model="claude-sonnet-5",
            latency_ms=1200,
        )

        await record_trace(session, trace)

        self.assertEqual(len(session.executed_queries), 1)
        self.assertEqual(session.commit_count, 1)
        _, params = session.executed_queries[0]
        self.assertEqual(params["tenant_id"], 1)
        self.assertEqual(params["conversation_id"], 3)
        self.assertEqual(params["intent"], "PRICE_INQUIRY")

    async def test_record_trace_includes_per_step_latency_and_error_category(self):
        """
        Regression test: db/init/007_observability.sql added per-step
        latency columns + error_category to agent_run_traces, and
        Kernel/CoreAgent compute all of them -- but the very first
        version of this INSERT (trace_service.py) was written before
        those columns existed and was never updated, so the data was
        silently discarded. This pins the fix: every one of those
        fields must actually reach the bound query parameters.
        """
        from app.trace.trace_service import record_trace
        from app.trace.trace_types import AgentRunTrace
        import uuid

        session = FakeSession([None])
        trace = AgentRunTrace(
            agent_run_id=uuid.uuid4(),
            tenant_id=1,
            customer_id=2,
            conversation_id=3,
            language_latency_ms=120,
            intent_latency_ms=340,
            context_latency_ms=80,
            memory_latency_ms=15,
            rag_latency_ms=60,
            llm_latency_ms=900,
            error_category="llm_error",
        )

        await record_trace(session, trace)

        _, params = session.executed_queries[0]
        self.assertEqual(params["language_latency_ms"], 120)
        self.assertEqual(params["intent_latency_ms"], 340)
        self.assertEqual(params["context_latency_ms"], 80)
        self.assertEqual(params["memory_latency_ms"], 15)
        self.assertEqual(params["rag_latency_ms"], 60)
        self.assertEqual(params["llm_latency_ms"], 900)
        self.assertEqual(params["error_category"], "llm_error")

    async def test_record_trace_includes_commercial_v1_lifecycle_fields(self):
        """
        Regression test for the same class of bug as the one above:
        db/init/008_trace_lifecycle.sql added trace_id, channel,
        status, failed_step, and steps to agent_run_traces, and
        Kernel/CoreAgent compute all of them -- pins that they reach
        the bound query parameters too.
        """
        from app.trace.trace_service import record_trace
        from app.trace.trace_types import AgentRunTrace
        import uuid

        session = FakeSession([None])
        steps = [
            {"step": "language", "status": "ok", "durationMs": 120},
            {"step": "rag", "status": "error", "error": "OpenAIError: [REDACTED]"},
        ]
        trace = AgentRunTrace(
            agent_run_id=uuid.uuid4(),
            tenant_id=1,
            customer_id=2,
            conversation_id=3,
            trace_id="req-abc-123",
            channel="whatsapp",
            status="partial",
            failed_step="rag",
            steps=steps,
        )

        await record_trace(session, trace)

        _, params = session.executed_queries[0]
        self.assertEqual(params["trace_id"], "req-abc-123")
        self.assertEqual(params["channel"], "whatsapp")
        self.assertEqual(params["status"], "partial")
        self.assertEqual(params["failed_step"], "rag")
        self.assertEqual(params["steps"], steps)

    async def test_record_trace_defaults_status_to_completed(self):
        """A trace built with no explicit status/steps (the common
        case -- a fully healthy run) must still default sensibly
        rather than requiring every caller to pass them."""
        from app.trace.trace_service import record_trace
        from app.trace.trace_types import AgentRunTrace
        import uuid

        session = FakeSession([None])
        trace = AgentRunTrace(agent_run_id=uuid.uuid4(), tenant_id=1, customer_id=2, conversation_id=3)

        await record_trace(session, trace)

        _, params = session.executed_queries[0]
        self.assertEqual(params["status"], "completed")
        self.assertIsNone(params["failed_step"])
        self.assertEqual(params["steps"], [])
        self.assertIsNone(params["trace_id"])
        self.assertIsNone(params["channel"])

    async def test_record_trace_failure_is_isolated_never_raises(self):
        from app.trace.trace_service import record_trace
        from app.trace.trace_types import AgentRunTrace
        import uuid

        class FailingSession(FakeSession):
            async def execute(self, query, params=None):
                raise RuntimeError("DB is down")

        session = FailingSession([])
        trace = AgentRunTrace(
            agent_run_id=uuid.uuid4(), tenant_id=1, customer_id=2, conversation_id=3
        )

        # Must not raise -- a trace-write failure can never affect the
        # customer-facing reply that already went out.
        try:
            await record_trace(session, trace)
        except Exception as exc:  # pragma: no cover -- test fails loudly if this ever raises
            self.fail(f"record_trace() raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
