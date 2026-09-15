"""
CAPABILITY REGISTRY.

Responsibility: hold CapabilityDefinitions in memory, keyed by name,
with register/get/list operations. This is a contract-holder, not an
executor -- it never calls `.handler`, never gets called from the
Kernel, and has no route/endpoint in this version.

Where this belongs in the existing architecture: this is the
foundation the future Tool Engine (docs/ROADMAP.md item 3) will read
from. It sits beside the Kernel (python-api/app/kernel/kernel.py),
which already has a marked insertion point for tool calls
(the `if intent_result.intent in (CREATE_ORDER, ORDER_STATUS)` block)
-- this registry is NOT wired to that insertion point in this version.
Per the task constraints: no autonomous tool execution, and "do not
implement real business tools yet unless an existing tool already
exists" -- no tool existed before this task, so the registry starts
and stays empty of real capabilities. Only a registry mechanism exists.
"""

from app.capabilities.capability_types import CapabilityDefinition


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDefinition] = {}

    def register(self, capability: CapabilityDefinition) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"Capability '{capability.name}' is already registered.")
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> CapabilityDefinition | None:
        return self._capabilities.get(name)

    def list_all(self) -> list[CapabilityDefinition]:
        return list(self._capabilities.values())

    def list_enabled(self) -> list[CapabilityDefinition]:
        return [c for c in self._capabilities.values() if c.enabled]


# Module-level singleton, matching the existing pattern (kernel, core_agent,
# language_engine, etc. are all module-level singletons in this codebase).
# Starts EMPTY -- nothing is registered here. Registering a capability
# does not make the Kernel call it; there is no consumer wired up yet.
capability_registry = CapabilityRegistry()
