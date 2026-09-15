"""
CAPABILITY REGISTRY — types.

Responsibility: define what a "Capability" (the future Tool Engine's
unit of work, e.g. check_product_stock, create_order) looks like as
DATA -- name, description, input contract, safety flags -- so the
contract exists and is stable before any capability is actually
implemented. This module defines NO executable tools and performs NO
autonomous tool execution (explicitly excluded from this task).
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class CapabilityDefinition:
    """
    A future-safe description of one capability the Kernel could
    eventually call. `handler` is optional and unused in this version
    -- see CapabilityRegistry's module docstring for why nothing is
    registered with a real handler yet.
    """

    name: str
    description: str
    # JSON-schema-shaped dict describing expected input, e.g.
    # {"product_id": "string", "quantity": "integer"} -- kept as a
    # plain dict (not a strict schema library) to stay minimal.
    input_schema: dict[str, str] = field(default_factory=dict)
    # If True, a future Decision Engine must require human/business-rule
    # approval before this capability may execute (architecture doc
    # section 24/34) -- not enforced anywhere yet, just declared.
    requires_confirmation: bool = True
    # Capabilities are declared but not necessarily runnable yet --
    # this lets the registry hold a capability's contract before its
    # implementation exists, without anything accidentally calling it.
    enabled: bool = False
    # Deliberately optional and unused in this version. A real handler
    # would be `Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]`
    # once the Tool Engine (docs/ROADMAP.md item 3) actually executes
    # capabilities -- Propose -> Permission Check -> Business Rule ->
    # Validate -> Execute, per the architecture doc. Nothing in this
    # codebase calls `.handler` in this version.
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
