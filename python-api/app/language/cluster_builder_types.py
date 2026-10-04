"""
CLUSTER BUILDER TYPES (Phase 3 — Automated Learning/Promotion Pipeline)

Data carriers for ClusterBuilderService and PromotionService.
No DB logic, no matching logic — only the shape of data that
passes between layers. Same single-purpose pattern as all other
*_types.py files in this package.
"""

from dataclasses import dataclass, field


@dataclass
class ExperienceRow:
    """
    One language_experiences row fetched for cluster building.
    Only the columns needed by ClusterBuilderService — not the full row.
    """
    experience_id: str
    final_intent: str
    normalized_message: str


@dataclass
class BuiltCluster:
    """
    One intent's candidate cluster as built by ClusterBuilderService.
    Produced by _build_for_intent(); collected into ClusterBuildResult.

    cluster_ids: the integer IDs of the newly inserted intent_clusters rows
        (one per representative example, up to TOP_K_EXAMPLES).

    example_count: len(cluster_ids) -- how many rows were written.

    total_samples: how many learning_eligible rows were examined for
        this intent (not just the top-k examples).
    """
    intent: str
    version: str
    example_count: int
    total_samples: int
    cluster_ids: list[int] = field(default_factory=list)


@dataclass
class ClusterBuildResult:
    """
    Summary returned by ClusterBuilderService.build_for_tenant().
    Informational only -- callers may log it.

    intents_built: how many intents got new candidate clusters.
    per_intent: one BuiltCluster per intent that was built.
    """
    tenant_id: int
    intents_built: int
    per_intent: list[BuiltCluster] = field(default_factory=list)


@dataclass
class PromotionCheckResult:
    """
    Result of PromotionService.check_and_promote() for one intent.

    action: what the promotion service decided:
        'promoted'              -- candidate met the accuracy threshold
            (and, if applicable, the §5.5 generalization gate), is now live
        'held_for_generalization' -- candidate met the accuracy threshold
            but failed the generalization gate (looks accurate on canary
            traffic, does not generalize); retired like a rollback but
            logged distinctly (see promotion_service.py)
        'rolled_back' -- canary fell below min_accuracy, reverted
        'pending'    -- not enough canary data yet, OR the generalization
            gate could not be run this cycle (retried next time);
            no action taken either way
        'no_canary'  -- no active canary for this intent
        'error'      -- unexpected failure (details in notes)

    was_correct_rate: routing_decisions.was_correct rate measured
        from canary traffic (None if no data or action='no_canary').

    cluster_id: the candidate cluster_id that was acted on
        (None if action='no_canary').

    notes: optional human-readable detail.
    """
    intent: str
    action: str       # 'promoted' | 'rolled_back' | 'pending' | 'no_canary' | 'error'
    was_correct_rate: float | None = None
    cluster_id: int | None = None
    notes: str | None = None
