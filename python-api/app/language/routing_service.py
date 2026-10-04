"""
ROUTING SERVICE (Phase 2 — Confidence Calibration + Live Routing)

docs/LANGUAGE_INTELLIGENCE.md Phase 2, step 2: given a BrainPrediction
from ShadowBrain, decide whether to route this turn to the brain
(skip the LLM Intent Engine, use predicted_intent directly) or to the
LLM (run the full Kernel as in Phase 1).

What this IS:
    A single, stateless function (decide()) that reads the calibration
    state from intent_clusters (via a lightweight DB lookup) and applies
    a deterministic routing rule:

        if the shadow brain made a prediction
        AND the cluster is calibrated (calibrated_threshold IS NOT NULL)
        AND similarity >= calibrated_threshold:
            → routed_to = 'brain'  (Phase 2 live routing; no per-intent
              business-risk exclusion here as of Phase 4 corrected
              scope -- see "Business-risk intents and brain-routing"
              below)
        else if the shadow brain made a prediction but threshold not met:
            → routed_to = 'llm'    (full Kernel, but brain result is still logged)
        else (no prediction at all):
            → routed_to = 'shadow' (Phase 1 backward-compat: log-only)

    The RoutingDecision returned by decide() is then written to
    routing_decisions by core_agent.py BEFORE the Kernel runs, so the
    routing choice is recorded even if the Kernel subsequently fails.

What this is NOT:
    - Not a trained classifier. No model weights. Pure threshold logic.
    - Not called from kernel.py. Only called from core_agent.py, in
      the same "after Phase 1 shadow, before or instead of Kernel"
      window -- specifically, decide() is called BEFORE kernel.run()
      so its result can be passed in as a hint.
    - Not responsible for confidence calibration (that is
      calibration_service.py). This service only READS calibrated
      state; it never updates intent_clusters.

Business-risk intents and brain-routing (CHANGED — Phase 4 corrected
scope, docs/GENERAL_LANGUAGE_BRAIN.md §3 / §3.1 / §9 item 1):
    CREATE_ORDER and ORDER_STATUS used to be permanently excluded from
    'brain' routing here (the old ROUTING_INELIGIBLE_INTENTS), on the
    reasoning that they "touch real money/fulfillment." That reasoning
    conflated two different questions: whether the language layer may
    recognize these intents fast (routing-for-UNDERSTANDING) vs.
    whether the system may autonomously ACT on them
    (automation_eligible, app/language/control_plane.py). Routing to
    'brain' only means the LLM Intent Engine call is skipped and the
    predicted intent is injected directly -- the Kernel still runs its
    full pipeline (RAG, Memory, State, LLM reply generation), and there
    is no Tool Engine yet that would let CREATE_ORDER/ORDER_STATUS
    autonomously execute anything (ROADMAP.md §3 marks that insertion
    point, still unbuilt). So these intents are no longer blocked from
    'brain' routing here -- they are calibrated and routed exactly like
    any other intent.

    What remains permanently blocked, regardless of how well language
    is understood, is autonomous ACTION on these intents --
    control_plane.AUTOMATION_INELIGIBLE_INTENTS -- and that gate
    belongs in the future Tool Engine (ROADMAP.md §3's
    Propose -> Permission Check -> Business Rule -> Validate -> Execute
    pipeline), not in this routing-for-understanding decision.

DB access:
    decide() makes one SELECT against intent_clusters per call (fetches
    the calibrated_threshold and agreement_rate for the predicted
    intent). This is intentionally kept as a single lightweight query
    rather than a cache, because:
        1. calibration_service updates intent_clusters out-of-band;
           a stale in-memory cache would silently route on old data.
        2. intent_clusters is small (one row per example per tenant per
           intent -- O(tens of rows) per tenant after seeding).
    If this SELECT becomes a bottleneck under real load, a short-lived
    per-tenant TTL cache can be introduced here without changing the
    interface -- but that is a later optimization, not a Phase 2
    concern.

Error handling:
    decide() never raises. Any DB failure or unexpected error returns
    a safe RoutingDecision(routed_to='llm', ...) -- fall through to the
    LLM. The Kernel is always the safe default.
"""

import random
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.language.calibration_types import RoutingDecision
from app.language.shadow_brain import SHADOW_BRAIN_VERSION
from app.language.shadow_brain_types import BrainPrediction

# REMOVED (Phase 4 corrected scope -- see this module's docstring):
# ROUTING_INELIGIBLE_INTENTS used to duplicate
# control_plane.AUTOMATION_INELIGIBLE_INTENTS and blocked
# CREATE_ORDER/ORDER_STATUS from 'brain' routing outright. Routing for
# understanding and authorizing action are separate control planes
# (docs/GENERAL_LANGUAGE_BRAIN.md §3) -- the business-risk exclusion
# that actually matters (never let these intents autonomously ACT) now
# lives in exactly one place: app/language/control_plane.py's
# AUTOMATION_INELIGIBLE_INTENTS, for the future Tool Engine to consult.


# ── Which cluster served a turn (PENDING_WORK C8, db/init/019) ─────────
BRANCH_CANDIDATE = "candidate"   # canary branch taken
BRANCH_PROMOTED = "promoted"     # an is_promoted cluster decided
BRANCH_OTHER = "other"           # calibrated, non-promoted (e.g. default seed)


class ServingCluster(NamedTuple):
    """Result of RoutingService._fetch_serving(); all-None = uncalibrated."""

    threshold: float | None = None
    agreement_rate: float | None = None
    cluster_id: int | None = None
    branch: str | None = None


class RoutingService:
    """
    Decides how to handle each turn: route to the brain (use
    predicted_intent directly), fall through to the LLM, or shadow-log
    only (Phase 1 backward-compat).

    Stateless -- one module-level singleton (routing_service below).
    All state lives in the DB (intent_clusters.calibrated_threshold).
    """

    async def decide(
        self,
        db: AsyncSession,
        tenant_id: int,
        brain_prediction: BrainPrediction | None,
    ) -> RoutingDecision:
        """
        Make a routing decision for this turn.

        Parameters
        ----------
        db              : active AsyncSession
        tenant_id       : must always travel with every DB call
        brain_prediction: result from ShadowBrain.predict() -- None
                          means no prediction was available this turn
                          (embedding failure, no clusters, empty message)

        Returns
        -------
        RoutingDecision
            routed_to='shadow' if brain_prediction is None (no-op --
                same as Phase 1 behavior, brain ran log-only if at all)
            routed_to='llm'    if prediction exists but calibration
                threshold is not met (or cluster is uncalibrated)
            routed_to='brain'  if prediction exists, calibrated, AND
                similarity >= threshold (no per-intent exclusion
                remains here -- see this module's docstring)
        """
        try:
            return await self._decide_inner(db, tenant_id, brain_prediction)
        except Exception:
            logger.error(
                "RoutingService.decide failed tenant=%s -- defaulting to LLM",
                tenant_id,
                exc_info=True,
            )
            # Safe fallback: full LLM path. Never raises.
            return RoutingDecision(
                routed_to="llm",
                predicted_intent=brain_prediction.predicted_intent if brain_prediction else None,
                similarity=brain_prediction.similarity if brain_prediction else None,
                brain_version=SHADOW_BRAIN_VERSION,
            )

    # ── Internal implementation ──────────────────────────────────────

    async def _decide_inner(
        self,
        db: AsyncSession,
        tenant_id: int,
        brain_prediction: BrainPrediction | None,
    ) -> RoutingDecision:
        # No prediction → shadow-only (same as Phase 1: brain either
        # wasn't called, failed, or had nothing to compare against).
        if brain_prediction is None:
            return RoutingDecision(
                routed_to="shadow",
                brain_version=SHADOW_BRAIN_VERSION,
            )

        predicted_intent = brain_prediction.predicted_intent
        similarity = brain_prediction.similarity

        # NOTE (Phase 4 corrected scope): CREATE_ORDER/ORDER_STATUS used
        # to be hard-excluded from brain routing here. They are no
        # longer special-cased in this method -- see this module's
        # docstring for why routing-for-understanding and
        # automation-authorization are separate concerns.

        # Fetch this cluster's calibrated threshold (if any).
        serving = await self._fetch_serving(db, tenant_id, predicted_intent)
        calibrated_threshold = serving.threshold
        agreement_rate = serving.agreement_rate

        # No calibrated threshold yet → shadow/llm.
        if calibrated_threshold is None:
            logger.debug(
                "RoutingService tenant=%s intent=%s not yet calibrated "
                "(similarity=%.3f) -- LLM",
                tenant_id,
                predicted_intent,
                similarity,
            )
            return RoutingDecision(
                routed_to="llm",
                predicted_intent=predicted_intent,
                similarity=similarity,
                calibrated_threshold=None,
                agreement_rate=None,
                brain_version=SHADOW_BRAIN_VERSION,
            )

        # Calibrated threshold exists: check if similarity meets it.
        if similarity >= calibrated_threshold:
            logger.info(
                "RoutingService tenant=%s intent=%s similarity=%.3f >= "
                "threshold=%.2f (agreement_rate=%.3f) -- BRAIN",
                tenant_id,
                predicted_intent,
                similarity,
                calibrated_threshold,
                agreement_rate or 0.0,
            )
            return RoutingDecision(
                routed_to="brain",
                predicted_intent=predicted_intent,
                similarity=similarity,
                calibrated_threshold=calibrated_threshold,
                agreement_rate=agreement_rate,
                brain_version=SHADOW_BRAIN_VERSION,
                served_cluster_id=serving.cluster_id,
                served_branch=serving.branch,
            )

        # Similarity below threshold → LLM.
        logger.debug(
            "RoutingService tenant=%s intent=%s similarity=%.3f < "
            "threshold=%.2f -- LLM",
            tenant_id,
            predicted_intent,
            similarity,
            calibrated_threshold,
        )
        return RoutingDecision(
            routed_to="llm",
            predicted_intent=predicted_intent,
            similarity=similarity,
            calibrated_threshold=calibrated_threshold,
            agreement_rate=agreement_rate,
            brain_version=SHADOW_BRAIN_VERSION,
            served_cluster_id=serving.cluster_id,
            served_branch=serving.branch,
        )

    # ── DB helpers ───────────────────────────────────────────────────

    async def _fetch_threshold(
        self,
        db: AsyncSession,
        tenant_id: int,
        intent: str,
    ) -> tuple[float | None, float | None]:
        """
        Backward-compatible (threshold, agreement_rate) view of
        _fetch_serving(). Kept so existing callers/tests need not change.
        """
        serving = await self._fetch_serving(db, tenant_id, intent)
        return serving.threshold, serving.agreement_rate

    async def _fetch_serving(
        self,
        db: AsyncSession,
        tenant_id: int,
        intent: str,
    ) -> "ServingCluster":
        """
        Decide which cluster's calibration serves this turn, and say which.

        Phase 3 — canary-aware:
        If an active canary split exists for this intent (canary_splits row),
        route canary_pct% of traffic to the candidate cluster's threshold
        and the rest to the promoted cluster's threshold. If no promoted
        cluster exists yet (still using default_seed), fall back to any
        calibrated row as before.

        C8: the result also carries WHICH cluster served the turn and in
        what role ('candidate' | 'promoted' | 'other'), so routing_decisions
        can record it and the canary ramp can count candidate-served turns
        only. Nothing about the routing choice itself changed.

        Returns an all-None ServingCluster if no calibrated threshold exists.
        """
        # Check for an active canary split
        canary = await db.execute(
            text("""
                SELECT cs.canary_pct, cs.candidate_cluster_id,
                       ic.calibrated_threshold AS cand_threshold,
                       ic.agreement_rate       AS cand_agreement
                  FROM canary_splits cs
                  JOIN intent_clusters ic ON ic.id = cs.candidate_cluster_id
                 WHERE cs.tenant_id = :tenant_id
                   AND cs.intent    = :intent
                 LIMIT 1
            """),
            {"tenant_id": tenant_id, "intent": intent},
        )
        canary_row = canary.first()

        if canary_row and getattr(canary_row, 'cand_threshold', None) is not None:
            # Canary active and candidate is calibrated.
            # Route canary_pct% to candidate, rest to promoted.
            if random.random() * 100 < canary_row.canary_pct:
                logger.debug(
                    "RoutingService canary branch tenant=%s intent=%s pct=%.1f%%",
                    tenant_id, intent, canary_row.canary_pct,
                )
                return ServingCluster(
                    threshold=float(canary_row.cand_threshold),
                    agreement_rate=(
                        float(canary_row.cand_agreement) if canary_row.cand_agreement else None
                    ),
                    cluster_id=getattr(canary_row, 'candidate_cluster_id', None),
                    branch=BRANCH_CANDIDATE,
                )

        # No canary, or canary branch not taken: use promoted or any calibrated cluster.
        result = await db.execute(
            text("""
                SELECT id, is_promoted, calibrated_threshold, agreement_rate
                  FROM intent_clusters
                 WHERE tenant_id = :tenant_id
                   AND intent    = :intent
                   AND calibrated_threshold IS NOT NULL
                 ORDER BY is_promoted DESC, agreement_rate DESC NULLS LAST
                 LIMIT 1
            """),
            {"tenant_id": tenant_id, "intent": intent},
        )
        row = result.first()
        if row is None:
            return ServingCluster()
        return ServingCluster(
            threshold=float(row.calibrated_threshold),
            agreement_rate=(
                float(row.agreement_rate) if row.agreement_rate is not None else None
            ),
            cluster_id=getattr(row, 'id', None),
            branch=(
                BRANCH_PROMOTED if getattr(row, 'is_promoted', False) is True
                else BRANCH_OTHER
            ),
        )


# Module-level singleton -- same pattern as every other engine in this
# codebase. Stateless between calls.
routing_service = RoutingService()
