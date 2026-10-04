"""
CALIBRATION TYPES (Phase 2 — Confidence Calibration + Live Routing)

Data carriers for the CalibrationService. Same single-purpose
type-carrier pattern already used by shadow_brain_types.py,
experience_types.py, and trace_types.py: no DB logic, no matching
logic -- only the shape of the data that passes between layers.

Read docs/LANGUAGE_INTELLIGENCE.md Phase 2 before touching this file.
"""

from dataclasses import dataclass, field


@dataclass
class IntentCalibrationStats:
    """
    Computed calibration statistics for one (tenant_id, intent) pair.
    Produced by CalibrationService._compute_stats_for_intent() and
    consumed by CalibrationService._update_clusters() to write back to
    intent_clusters.

    similarity_threshold: the minimum cosine similarity at which the
        observed agreement_rate meets or exceeds MIN_AGREEMENT_RATE.
        None means "not enough data or never above MIN_AGREEMENT_RATE
        at any threshold tried" -- the cluster stays shadow-only.

    agreement_rate: observed fraction of brain_used=TRUE turns where
        brain_prediction.agreement was TRUE, for turns where similarity
        >= similarity_threshold. None when similarity_threshold is None.

    sample_count: total brain_used=TRUE rows examined for this intent
        across all thresholds (not filtered by threshold). The
        calibration service requires this to meet MIN_SAMPLE_COUNT
        before writing a threshold (even if agreement_rate looks good
        on a tiny sample).
    """

    intent: str
    similarity_threshold: float | None
    agreement_rate: float | None
    sample_count: int


@dataclass
class CalibrationResult:
    """
    Summary returned by CalibrationService.run_for_tenant(). Purely
    informational -- callers may log it; nothing in the codebase
    branches on it yet.

    intents_updated: how many (intent, cluster) rows had their
        calibrated_threshold / agreement_rate updated (including
        rows explicitly reset to NULL when sample_count dropped
        below MIN_SAMPLE_COUNT).

    total_samples: total brain_used=TRUE rows examined for this tenant
        (sum across all intents).

    per_intent: one IntentCalibrationStats per intent that had at least
        one brain_used=TRUE sample. Intents with no shadow data at all
        are not included (nothing to compute or write back).
    """

    tenant_id: int
    intents_updated: int
    total_samples: int
    per_intent: list[IntentCalibrationStats] = field(default_factory=list)


@dataclass
class RoutingDecision:
    """
    The routing choice made by RoutingService.decide() for one turn.
    Written to routing_decisions table by core_agent.py before the
    Kernel runs.

    routed_to: one of 'brain' | 'llm' | 'shadow'
        'brain'  -- calibrated, high-confidence: predicted_intent is
                    used directly; the full LLM Intent Engine step is
                    skipped.  (Phase 2, live routing)
        'llm'    -- not calibrated or low similarity: full Kernel runs
                    as in Phase 1.
        'shadow' -- Phase 1 backward-compat: brain ran alongside the
                    LLM but its prediction was log-only.

    predicted_intent: the shadow brain's predicted intent at decision
        time (None when no prediction was available).

    similarity: raw cosine similarity from ShadowBrain.predict()
        (None when no prediction was available).

    calibrated_threshold: the threshold snapshot used at decision time
        (None when the cluster is not yet calibrated).

    agreement_rate: the agreement_rate snapshot at decision time
        (None when uncalibrated).

    brain_version: SHADOW_BRAIN_VERSION at decision time.

    served_cluster_id / served_branch (db/init/019, PENDING_WORK C8):
        which intent_clusters row's threshold decided this turn, and its
        role -- 'candidate' | 'promoted' | 'other'. Both None when no
        cluster decided (no prediction / uncalibrated). The canary ramp
        counts only served_branch='candidate' rows as candidate evidence.
    """

    routed_to: str          # 'brain' | 'llm' | 'shadow'
    predicted_intent: str | None = None
    similarity: float | None = None
    calibrated_threshold: float | None = None
    agreement_rate: float | None = None
    brain_version: str | None = None
    served_cluster_id: int | None = None
    served_branch: str | None = None
