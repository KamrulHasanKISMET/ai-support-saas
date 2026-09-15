from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class AgentConfig:
    """
    Runtime configuration for a single Core Agent execution.

    This is deliberately thin in v1 -- it exists so tenant/vertical-specific
    knobs (e.g. which vertical's agent this is, default reply language,
    whether tool-calling is enabled yet) have a home that is NOT the
    Kernel and NOT scattered across engines. Loaded per-request from the
    `agent_configs` Postgres table by load_agent_config() below -- see
    db/init/002_core_tables.sql for the table definition.

    Per the integration doc (section 30): vertical-specific behavior
    should come from configuration/knowledge/business rules, not from
    duplicating Core Agent or Kernel code.
    """

    vertical: str = "ecommerce"
    tools_enabled: bool = False


DEFAULT_AGENT_CONFIG = AgentConfig()


async def load_agent_config(db: AsyncSession, tenant_id: int) -> AgentConfig:
    """
    Loads this tenant's row from `agent_configs`, if one exists.

    Returns a FRESH AgentConfig each call rather than mutating a shared
    instance -- CoreAgent is a module-level singleton handling
    concurrent requests for many tenants at once, so storing per-tenant
    state on `self` would let one tenant's config leak into another
    tenant's in-flight request. Always thread the result through as a
    local variable, never assign it back onto CoreAgent.config.
    """
    result = await db.execute(
        text(
            "SELECT vertical, config FROM agent_configs WHERE tenant_id = :tenant_id"
        ),
        {"tenant_id": tenant_id},
    )
    row = result.first()
    if row is None:
        return AgentConfig()

    config_json = row.config or {}
    return AgentConfig(
        vertical=row.vertical or DEFAULT_AGENT_CONFIG.vertical,
        tools_enabled=bool(
            config_json.get("tools_enabled", DEFAULT_AGENT_CONFIG.tools_enabled)
        ),
    )
