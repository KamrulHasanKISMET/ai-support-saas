"""
TENANT BRAIN — architectural foundation only, NOT wired into
CoreAgent/Kernel in this version (see docs/AGENT.md).

Responsibility: the "Tenant Brain" is the single place that composes
everything that makes one tenant's agent behave differently from
another tenant's — its AgentConfig (vertical, tools_enabled, ...) plus
its active business_rules. It does not decide anything itself and does
not talk to an LLM; it is a read-only aggregation of existing,
already-tenant-scoped tables.

Core principle this implements (per the extension spec):
    Shared Core Agent + Tenant-specific configuration/knowledge/
    business rules = Tenant-specific AI behavior

Reuses, does not duplicate:
    - agent_configs (via the existing load_agent_config()) for
      vertical/tools_enabled.
    - business_rules (new read here — this table existed already but
      nothing read it before this file).

Why this is NOT wired into CoreAgent.run() yet: doing so would add a
business_rules query to every single message, for data nothing
currently consumes (no Decision Engine exists to apply a business rule
to anything). That's a real behavior/cost change for zero benefit today
-- so per "do not activate future behavior unless explicitly
implemented and tested" (spec rule 5), this stays a standalone,
independently-loadable, independently-tested module until a real
consumer (the future Decision Engine) needs it.
"""

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.agent_config import AgentConfig, load_agent_config


@dataclass
class BusinessRule:
    """One row from `business_rules`, read-only."""

    rule_key: str
    rule_value: dict


@dataclass
class TenantBrain:
    """
    Everything that makes this tenant's agent tenant-specific, composed
    from existing tables. Nothing here overrides customer intent or
    Kernel behavior -- it is descriptive data, not a decision.
    """

    tenant_id: int
    agent_config: AgentConfig
    business_rules: list[BusinessRule] = field(default_factory=list)

    def get_rule(self, rule_key: str) -> dict | None:
        """Convenience lookup a future Decision Engine would use --
        e.g. tenant_brain.get_rule('max_discount_percent')."""
        for rule in self.business_rules:
            if rule.rule_key == rule_key:
                return rule.rule_value
        return None


async def load_tenant_brain(db: AsyncSession, tenant_id: int) -> TenantBrain:
    """
    Loads a tenant's full brain: AgentConfig (reusing the existing
    loader, not duplicating its query) + active business_rules.

    Defensive: if the business_rules query fails, returns an empty
    rules list rather than raising -- a caller that only needs
    agent_config should not be broken by an unrelated table's failure.
    AgentConfig loading failures are NOT caught here; that's already
    handled by load_agent_config()'s own caller (CoreAgent) if/when
    this function is ever wired in.
    """
    agent_config = await load_agent_config(db, tenant_id)

    try:
        result = await db.execute(
            text(
                """
                SELECT rule_key, rule_value
                  FROM business_rules
                 WHERE tenant_id = :tenant_id AND is_active = TRUE
                """
            ),
            {"tenant_id": tenant_id},
        )
        rules = [
            BusinessRule(rule_key=row.rule_key, rule_value=row.rule_value or {})
            for row in result
        ]
    except Exception:
        rules = []

    return TenantBrain(tenant_id=tenant_id, agent_config=agent_config, business_rules=rules)
