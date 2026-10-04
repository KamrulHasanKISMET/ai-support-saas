"""
CLUSTER BUILDER SERVICE (Phase 3 — Automated Learning/Promotion Pipeline;
row-selection gate MODIFIED per Phase 4 corrected scope, §9 item 2)

docs/LANGUAGE_INTELLIGENCE.md Phase 3, step 1: promote real
language_experiences rows into new intent_clusters rows
(source='tenant_learned'), replacing the generic default_seed examples
that Phase 1 bootstrapped.

Row-selection gate (docs/GENERAL_LANGUAGE_BRAIN.md §2.3 — CHANGED):
    Previously this service selected rows with `learning_eligible=TRUE`
    alone. That flag now only answers "is this intent's LANGUAGE
    pattern learnable at all" (control_plane.is_language_learning_eligible,
    always TRUE for every intent today) -- it says nothing about whether
    any INDIVIDUAL row's `final_intent` label has actually been
    verified. Treating "the LLM said so" as automatically trustworthy
    training material is the exact anti-pattern Correction #2
    prohibits. The gate is now the evidence hierarchy's eligibility
    rule (§2.3):

        verification_level IN ('outcome_positive', 'human_confirmed')
          OR (verification_level = 'self_consistent'
              AND source_reliability >= tenant_config.min_reliability)

    ...applied IN ADDITION to (not instead of) learning_eligible=TRUE,
    intent-level promotion_eligible, and superseded_by IS NULL. See
    `_fetch_eligible_rows()` below for the exact query.

What this IS:
    Reads eligible language_experiences rows (per the gate above) and,
    for each (tenant_id, intent) pair with enough samples, selects
    representative examples to use as the new nearest-neighbor
    cluster. Writes new intent_clusters rows (source='tenant_learned',
    is_candidate=TRUE) and logs a cluster_promotion_log event
    ('shadow_eval'). Embedding is delegated entirely to
    embedding_service.embed() -- same as shadow_brain.py.

What this is NOT:
    - Not a training loop. No model weights change.
    - Not PromotionService (which governs canary → promote → rollback).
      This service only BUILDS candidate clusters; promotion is separate.
    - Not called from the hot request path. Called out-of-band (scheduled
      task or management route) after enough shadow data has accumulated.
    - Not responsible for retiring or deleting old clusters. That is
      PromotionService's job.

Example selection strategy ("centroid-nearest"):
    1. Embed all eligible normalized_messages for this intent.
    2. Compute the centroid (mean embedding).
    3. Select the TOP_K_EXAMPLES messages nearest to the centroid.
       These are the most "representative" turns -- closest to the
       average of what real customers said for this intent.
    4. Write them as new intent_clusters rows.

Why centroid-nearest over random sampling:
    - Random samples may pick edge cases or unusual phrasings.
    - Centroid-nearest picks the most typical examples, so the
      nearest-neighbor matcher (shadow_brain.py) has the best chance
      of matching future turns correctly.
    - Avoids the need for a clustering algorithm (k-means, etc.) which
      would add complexity and hyperparameters.

Parameters (class-level constants):

    MIN_SAMPLES_TO_BUILD = 50
        Minimum learning_eligible rows required per intent before a
        candidate cluster is built. Below this, the existing cluster
        (default_seed or previous tenant_learned) stays in place.

    TOP_K_EXAMPLES = 10
        Number of representative examples to select per intent.
        This is also the number of intent_clusters rows written.
        Matches the default_seed size from shadow_brain.py's
        _DEFAULT_SEED_EXAMPLES (kept consistent so the matcher
        has the same number of examples to compare against regardless
        of whether it's using seed or learned clusters).

    VERSION_PREFIX = "tenant_learned"
        Source tag written to intent_clusters.source and used as the
        prefix for intent_clusters.version strings.
"""

import math
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embedding_service import embedding_service
from app.core.logging import logger
from app.language import control_plane
from app.language.cluster_builder_types import (
    BuiltCluster,
    ClusterBuildResult,
    ExperienceRow,
)

MIN_SAMPLES_TO_BUILD: int = 50
TOP_K_EXAMPLES: int = 10
VERSION_PREFIX: str = "tenant_learned"


class ClusterBuilderService:
    """
    Builds candidate intent_clusters rows from real language_experiences.

    One module-level singleton (cluster_builder_service below). Stateless
    between calls. Safe to call concurrently for different tenants; avoid
    concurrent calls for the same tenant (last-write-wins on candidate rows).
    """

    async def build_for_tenant(
        self,
        db: AsyncSession,
        tenant_id: int,
    ) -> ClusterBuildResult:
        """
        Build candidate clusters for all eligible intents for one tenant.

        Returns ClusterBuildResult. Never raises -- failures per intent are
        logged and skipped; a fatal failure returns an empty result.
        """
        try:
            return await self._build(db, tenant_id)
        except Exception:
            logger.error(
                "ClusterBuilderService.build_for_tenant failed fatally tenant=%s",
                tenant_id, exc_info=True,
            )
            return ClusterBuildResult(tenant_id=tenant_id, intents_built=0, per_intent=[])

    # ── Internal ─────────────────────────────────────────────────────

    async def _build(self, db: AsyncSession, tenant_id: int) -> ClusterBuildResult:
        rows = await self._fetch_eligible_rows(db, tenant_id)
        if not rows:
            logger.info(
                "ClusterBuilderService tenant=%s no learning_eligible rows yet", tenant_id
            )
            return ClusterBuildResult(tenant_id=tenant_id, intents_built=0, per_intent=[])

        # Group by final_intent
        by_intent: dict[str, list[ExperienceRow]] = {}
        for row in rows:
            by_intent.setdefault(row.final_intent, []).append(row)

        built: list[BuiltCluster] = []
        for intent, intent_rows in by_intent.items():
            if not control_plane.is_promotion_eligible(intent):
                # Defensive -- PROMOTION_INELIGIBLE_INTENTS is empty
                # today (control_plane.py), so this never actually
                # skips anything yet. Kept as the single place a
                # future exclusion would take effect, rather than
                # leaving cluster-building with no promotion_eligible
                # check at all.
                logger.info(
                    "ClusterBuilderService tenant=%s intent=%s is promotion-ineligible -- skipping",
                    tenant_id, intent,
                )
                continue
            if len(intent_rows) < MIN_SAMPLES_TO_BUILD:
                logger.info(
                    "ClusterBuilderService tenant=%s intent=%s only %d samples (need %d) -- skipping",
                    tenant_id, intent, len(intent_rows), MIN_SAMPLES_TO_BUILD,
                )
                continue
            try:
                cluster = await self._build_for_intent(db, tenant_id, intent, intent_rows)
                if cluster:
                    built.append(cluster)
            except Exception:
                logger.error(
                    "ClusterBuilderService failed for intent=%s tenant=%s -- skipping",
                    intent, tenant_id, exc_info=True,
                )

        logger.info(
            "ClusterBuilderService.build_for_tenant done tenant=%s intents_built=%d",
            tenant_id, len(built),
        )
        return ClusterBuildResult(
            tenant_id=tenant_id,
            intents_built=len(built),
            per_intent=built,
        )

    async def _build_for_intent(
        self,
        db: AsyncSession,
        tenant_id: int,
        intent: str,
        rows: list["ExperienceRow"],
    ) -> "BuiltCluster | None":
        """
        Embed all rows, pick centroid-nearest TOP_K_EXAMPLES, write
        new intent_clusters rows as is_candidate=TRUE.
        """
        messages = [r.normalized_message for r in rows]

        # Embed all messages (batch)
        embeddings = await embedding_service.embed_batch(messages)
        if not embeddings or len(embeddings) != len(messages):
            logger.error(
                "ClusterBuilderService embedding batch failed tenant=%s intent=%s",
                tenant_id, intent,
            )
            return None

        # Compute centroid
        dim = len(embeddings[0])
        centroid = [0.0] * dim
        for emb in embeddings:
            for i, v in enumerate(emb):
                centroid[i] += v
        centroid = [v / len(embeddings) for v in centroid]

        # Score each embedding by cosine similarity to centroid
        centroid_norm = math.sqrt(sum(v * v for v in centroid))

        def cosine_to_centroid(emb: list[float]) -> float:
            dot = sum(a * b for a, b in zip(emb, centroid))
            emb_norm = math.sqrt(sum(v * v for v in emb))
            if emb_norm == 0 or centroid_norm == 0:
                return 0.0
            return dot / (emb_norm * centroid_norm)

        scored = sorted(
            zip(rows, embeddings),
            key=lambda pair: cosine_to_centroid(pair[1]),
            reverse=True,
        )
        top_k = scored[:TOP_K_EXAMPLES]

        # Build version string
        version = f"{VERSION_PREFIX}-v1-{datetime.utcnow().strftime('%Y%m%d')}"

        # Retire any existing is_candidate rows for this intent
        await self._retire_existing_candidates(db, tenant_id, intent)

        # Write new candidate cluster rows
        cluster_ids = []
        for row, emb in top_k:
            cluster_id = await self._insert_cluster_row(
                db,
                tenant_id=tenant_id,
                intent=intent,
                example=row.normalized_message,
                embedding=emb,
                version=version,
                source_experience_id=row.experience_id,
            )
            cluster_ids.append(cluster_id)

        # Log shadow_eval event
        await self._log_promotion_event(
            db,
            tenant_id=tenant_id,
            intent=intent,
            cluster_id=cluster_ids[0] if cluster_ids else None,
            version=version,
            event_type="shadow_eval",
            sample_count=len(rows),
        )

        logger.info(
            "ClusterBuilderService built candidate tenant=%s intent=%s "
            "version=%s examples=%d total_samples=%d",
            tenant_id, intent, version, len(cluster_ids), len(rows),
        )

        return BuiltCluster(
            intent=intent,
            version=version,
            example_count=len(cluster_ids),
            total_samples=len(rows),
            cluster_ids=cluster_ids,
        )

    # ── DB helpers ───────────────────────────────────────────────────

    async def _fetch_eligible_rows(
        self, db: AsyncSession, tenant_id: int
    ) -> list["ExperienceRow"]:
        """
        Fetch rows that clear BOTH gates:

          1. learning_eligible=TRUE (control_plane.is_language_learning_eligible
             at write time -- today TRUE for every intent) with a
             non-null normalized_message and final_intent, not
             superseded by a later label revision.
          2. The verification_level eligibility rule (§2.3): a row is
             only an eligible LEARNING signal (as opposed to merely
             logged) once it is 'outcome_positive'/'human_confirmed',
             or 'self_consistent' with source_reliability at or above
             this tenant's configured min_reliability (defaulting to
             0.90 when the tenant has no tenant_calibration_config row
             at all -- same default-via-LEFT-JOIN pattern
             routing_service.py's canary lookup does not need, but
             calibration_service.py's tenant-config reads do).

        This replaces the old `learning_eligible=TRUE` alone -- see
        this module's docstring for why that was insufficient.
        """
        result = await db.execute(
            text("""
                SELECT le.experience_id,
                       le.final_intent,
                       le.normalized_message
                  FROM language_experiences le
                  LEFT JOIN tenant_calibration_config tcc
                         ON tcc.tenant_id = le.tenant_id
                 WHERE le.tenant_id          = :tenant_id
                   AND le.learning_eligible  = TRUE
                   AND le.normalized_message IS NOT NULL
                   AND le.final_intent       IS NOT NULL
                   AND le.superseded_by      IS NULL
                   AND (
                        le.verification_level IN ('outcome_positive', 'human_confirmed')
                        OR (
                            le.verification_level = 'self_consistent'
                            AND le.source_reliability >= COALESCE(tcc.min_reliability, 0.90)
                        )
                   )
                 ORDER BY le.created_at ASC
            """),
            {"tenant_id": tenant_id},
        )
        return [
            ExperienceRow(
                experience_id=str(row.experience_id),
                final_intent=row.final_intent,
                normalized_message=row.normalized_message,
            )
            for row in result
        ]

    async def _retire_existing_candidates(
        self, db: AsyncSession, tenant_id: int, intent: str
    ) -> None:
        """Clear is_candidate on any existing candidate rows for this intent."""
        await db.execute(
            text("""
                UPDATE intent_clusters
                   SET is_candidate = FALSE,
                       retired_at   = NOW()
                 WHERE tenant_id   = :tenant_id
                   AND intent      = :intent
                   AND is_candidate = TRUE
            """),
            {"tenant_id": tenant_id, "intent": intent},
        )
        await db.commit()

    async def _insert_cluster_row(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        intent: str,
        example: str,
        embedding: list[float],
        version: str,
        source_experience_id: str,
    ) -> int:
        """Insert one intent_clusters row as a new candidate. Returns its id."""
        emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
        result = await db.execute(
            text("""
                INSERT INTO intent_clusters (
                    tenant_id, intent, example, embedding,
                    source, version, is_candidate, is_promoted
                ) VALUES (
                    :tenant_id, :intent, :example, :embedding::vector,
                    :source, :version, TRUE, FALSE
                )
                RETURNING id
            """),
            {
                "tenant_id": tenant_id,
                "intent": intent,
                "example": example,
                "embedding": emb_str,
                "source": VERSION_PREFIX,
                "version": version,
            },
        )
        await db.commit()
        row = result.first()
        return row.id if row else -1

    async def _log_promotion_event(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        intent: str,
        cluster_id: int | None,
        version: str,
        event_type: str,
        sample_count: int,
        agreement_rate: float | None = None,
        was_correct_rate: float | None = None,
        canary_pct: float | None = None,
        notes: str | None = None,
    ) -> None:
        try:
            await db.execute(
                text("""
                    INSERT INTO cluster_promotion_log (
                        tenant_id, intent, cluster_id, version, event_type,
                        sample_count, agreement_rate, was_correct_rate, canary_pct, notes
                    ) VALUES (
                        :tenant_id, :intent, :cluster_id, :version, :event_type,
                        :sample_count, :agreement_rate, :was_correct_rate, :canary_pct, :notes
                    )
                """),
                {
                    "tenant_id": tenant_id, "intent": intent, "cluster_id": cluster_id,
                    "version": version, "event_type": event_type,
                    "sample_count": sample_count, "agreement_rate": agreement_rate,
                    "was_correct_rate": was_correct_rate, "canary_pct": canary_pct,
                    "notes": notes,
                },
            )
            await db.commit()
        except Exception:
            logger.error(
                "ClusterBuilderService failed to log promotion event tenant=%s intent=%s",
                tenant_id, intent, exc_info=True,
            )


cluster_builder_service = ClusterBuilderService()
