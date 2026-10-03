import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.agent_config import (
    AgentConfig,
    DEFAULT_AGENT_CONFIG,
    load_agent_config,
)
from app.core.config import settings
from app.core.logging import logger
from app.kernel.kernel import kernel
from app.language import control_plane, glb
from app.language.calibration_types import RoutingDecision
from app.language.conversation_cost_tracker import conversation_cost_tracker
from app.language.experience_service import record_language_experience
from app.language.experience_types import LanguageExperience
from app.language.glb_orchestrator import plan_to_dict, plan_turn
from app.language import model_serving, model_shadow
from app.language.novelty_detector import novelty_detector
from app.language.routing_service import routing_service
from app.language.shadow_brain import SHADOW_BRAIN_VERSION, shadow_brain
from app.language.understanding_store import record_turn_understanding
from app.schemas.agent import AgentRequest, AgentResponse
from app.trace.trace_service import record_trace
from app.trace.trace_types import AgentRunTrace


# Corrected scope (docs/GENERAL_LANGUAGE_BRAIN.md §3.1, §9 item 1):
# language-learning eligibility is decided by app/language/control_plane.py,
# NOT by a locally copy-pasted business-risk set. Previously this module
# defined its own `_LEARNING_INELIGIBLE_INTENTS = {CREATE_ORDER, ORDER_STATUS}`
# and excluded those intents from learning_eligible -- that was the literal
# bug Correction #3 calls out: "amar parcel ta koi?" is exactly the kind of
# phrasing the Brain should learn, even though (per
# control_plane.is_automation_eligible()) it must never be autonomously
# acted on. See control_plane.py's module docstring for the full rationale.


# =====================================================================
# ROUTING DECISION PERSISTENCE
#
# This is intentionally module-level so tests and other runtime code can
# access the persistence helper independently from CoreAgent.
#
# The helper is isolated: a routing_decisions write failure must never
# affect the customer reply.
#
# `served_cluster_id` and `served_branch` are optional for compatibility
# with older RoutingDecision objects/mocks.
# =====================================================================

async def _record_routing_decision(
    db: AsyncSession,
    *,
    tenant_id: int,
    customer_id: int,
    conversation_id: int,
    message_id: int | None,
    request_id: str | None,
    experience_id: uuid.UUID,
    decision: RoutingDecision,
    final_intent: str | None,
) -> None:
    """
    Write one routing_decisions row (db/init/012_calibration.sql).

    Isolated try/except -- a write failure here never affects the reply.

    was_correct is only meaningful when routed_to='brain' (we predicted
    an intent and the Kernel confirmed or contradicted it). For 'llm'
    and 'shadow' routes, was_correct is NULL -- we didn't commit to a
    prediction, so there is nothing to be "correct" or "incorrect"
    about.
    """
    try:
        was_correct: bool | None = None

        if (
            decision.routed_to == "brain"
            and decision.predicted_intent is not None
        ):
            was_correct = decision.predicted_intent == final_intent

        await db.execute(
            text(
                """
                INSERT INTO routing_decisions (
                    tenant_id,
                    customer_id,
                    conversation_id,
                    message_id,
                    request_id,
                    experience_id,
                    routed_to,
                    predicted_intent,
                    similarity,
                    calibrated_threshold,
                    agreement_rate,
                    brain_version,
                    final_intent,
                    was_correct,
                    served_cluster_id,
                    served_branch
                )
                VALUES (
                    :tenant_id,
                    :customer_id,
                    :conversation_id,
                    :message_id,
                    :request_id,
                    :experience_id,
                    :routed_to,
                    :predicted_intent,
                    :similarity,
                    :calibrated_threshold,
                    :agreement_rate,
                    :brain_version,
                    :final_intent,
                    :was_correct,
                    :served_cluster_id,
                    :served_branch
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "request_id": request_id,
                "experience_id": str(experience_id),
                "routed_to": decision.routed_to,
                "predicted_intent": decision.predicted_intent,
                "similarity": decision.similarity,
                "calibrated_threshold": decision.calibrated_threshold,
                "agreement_rate": decision.agreement_rate,
                "brain_version": decision.brain_version,
                "final_intent": final_intent,
                "was_correct": was_correct,
                # db/init/019: which cluster's threshold decided this turn.
                # getattr keeps older RoutingDecision objects/tests working.
                "served_cluster_id": getattr(
                    decision,
                    "served_cluster_id",
                    None,
                ),
                "served_branch": getattr(
                    decision,
                    "served_branch",
                    None,
                ),
            },
        )
        await db.commit()

    except Exception:
        logger.error(
            "Failed to record routing_decision tenant=%s conv=%s",
            tenant_id,
            conversation_id,
            exc_info=True,
        )


# =====================================================================
# CORE AGENT
#
# What this IS:
#   The reusable runtime entry point that receives a customer
#   interaction and hands it to the existing Kernel. It is the stable
#   "front door" for every AI-powered interaction, regardless of
#   channel or business vertical.
#
# What this is NOT:
#   - not the LLM, not RAG, not Memory, not the Context/Intent/State
#     engines, not FastAPI, not Node.js.
#   - not an orchestrator itself -- orchestration stays in
#     app/kernel/kernel.py exactly as it already is.
#
# Language additions:
#   - Shadow Brain prediction
#   - RoutingService
#   - Language Experience Store
#   - Novelty Detection
#   - Conversation Cost Tracking
#   - GLB per-turn understanding
#   - Advisory GLB plan
#   - Model Serving
#   - Model Shadow
#
# The Kernel remains the owner of the actual interaction pipeline.
# =====================================================================


class CoreAgent:
    def __init__(self, config: AgentConfig = DEFAULT_AGENT_CONFIG):
        # Fallback only, used if load_agent_config() itself fails below.
        # NOT mutated per-request -- see load_agent_config()'s docstring
        # for why per-tenant config must stay a local variable.
        self._fallback_config = config

    async def run(
        self,
        db: AsyncSession,
        request: AgentRequest,
    ) -> AgentResponse:
        agent_run_id = uuid.uuid4()
        experience_id = uuid.uuid4()
        started_at = time.perf_counter()

        try:
            tenant_config = await load_agent_config(
                db,
                request.tenantId,
            )
        except Exception:
            logger.error(
                "Failed to load agent_configs for tenant=%s -- using default",
                request.tenantId,
                exc_info=True,
            )
            tenant_config = self._fallback_config

        logger.info(
            "CoreAgent.run agent_run_id=%s tenant=%s customer=%s "
            "conversation=%s channel=%s vertical=%s tools_enabled=%s",
            agent_run_id,
            request.tenantId,
            request.customerId,
            request.conversationId,
            request.channel,
            tenant_config.vertical,
            tenant_config.tools_enabled,
        )

        # ==============================================================
        # Phase 1: Shadow Brain prediction
        # ==============================================================
        #
        # Run BEFORE the Kernel so RoutingService can make a decision.
        # shadow_brain.predict() is expected to fail safely and return
        # None when no prediction is available.
        #
        # The raw message is intentionally used here because the Kernel's
        # Language Engine performs the authoritative normalization.
        #
        brain_result = None

        if request.message and request.message.strip():
            brain_result = await shadow_brain.predict(
                db,
                request.tenantId,
                request.message,
            )

        # ==============================================================
        # Phase 2: Routing decision
        # ==============================================================
        #
        # RoutingService decides whether the turn would be:
        #   brain / llm / shadow
        #
        # Phase 2 is intentionally safe:
        # the Kernel still runs normally. The routing result is recorded
        # and, when appropriate, passed as a validated intent hint.
        #
        routing_decision = await routing_service.decide(
            db,
            request.tenantId,
            brain_result,
        )

        logger.info(
            "RoutingService decision tenant=%s conv=%s routed_to=%s "
            "predicted=%s similarity=%s threshold=%s",
            request.tenantId,
            request.conversationId,
            routing_decision.routed_to,
            routing_decision.predicted_intent,
            (
                f"{routing_decision.similarity:.3f}"
                if routing_decision.similarity is not None
                else None
            ),
            (
                f"{routing_decision.calibrated_threshold:.2f}"
                if routing_decision.calibrated_threshold is not None
                else None
            ),
        )

        # ==============================================================
        # Kernel run
        # ==============================================================
        #
        # The Kernel remains the actual owner of:
        # Intent / State / Context / RAG / Memory / LLM / Reply.
        #
        # The routing brain hint is only advisory and must be validated
        # by the Kernel.
        #
        brain_hint = (
            routing_decision.predicted_intent
            if routing_decision.routed_to == "brain"
            else None
        )

        # ==============================================================
        # Phase 6: Model Serving
        # ==============================================================
        #
        # OFF by default.
        # Only considered when:
        #   - model serving is enabled
        #   - cluster Brain did not already provide a hint
        #   - request contains usable text
        #
        # It can only add a hint which the Kernel validates.
        #
        model_serve = None

        if (
            settings.model_serving_enabled
            and brain_hint is None
            and request.message
            and request.message.strip()
        ):
            from app.ai.embedding_service import embedding_service

            model_serve = await model_serving.decide(
                db,
                enabled=True,
                canary_enabled=settings.model_serving_canary_enabled,
                tenant_id=request.tenantId,
                text=request.message,
                embed_fn=embedding_service.embed_batch,
                embedding_model=embedding_service.model,
            )

            if (
                model_serve is not None
                and model_serve.serve
                and model_serve.use_hint
            ):
                brain_hint = model_serve.intent

        # ==============================================================
        # Existing Kernel
        # ==============================================================
        kernel_result = await kernel.run(
            db=db,
            tenant_id=request.tenantId,
            customer_id=request.customerId,
            conversation_id=request.conversationId,
            message=request.message,
            request_id=request.requestId,
            predicted_intent_hint=brain_hint,
        )

        latency_ms = int(
            (time.perf_counter() - started_at) * 1000
        )

        # ==============================================================
        # Phase 6: Model Serving audit record
        # ==============================================================
        if model_serve is not None and model_serve.serve:
            await model_serving.record_served(
                db,
                tenant_id=request.tenantId,
                conversation_id=request.conversationId,
                experience_id=experience_id,
                decision=model_serve,
                final_intent=kernel_result.intent,
                kernel_used_hint=(
                    kernel_result.intentSource == "brain"
                ),
                language=getattr(
                    kernel_result,
                    "language",
                    None,
                ),
            )

        # ==============================================================
        # Trace correlation
        # ==============================================================
        #
        # Preserve MASTER behavior:
        # reuse request_id when available; otherwise generate a trace ID.
        #
        trace_id = request.requestId or str(uuid.uuid4())

        # ==============================================================
        # Agent Run Trace
        # ==============================================================
        #
        # Preserve the complete MASTER AgentRunTrace payload.
        #
        await record_trace(
            db,
            AgentRunTrace(
                agent_run_id=agent_run_id,
                tenant_id=request.tenantId,
                customer_id=request.customerId,
                conversation_id=request.conversationId,
                message_id=request.messageId,
                request_id=request.requestId,
                detected_language=kernel_result.detectedLanguage,
                reply_language=kernel_result.replyLanguage,
                normalized_input=kernel_result.normalizedMessage,
                intent=kernel_result.intent,
                confidence=kernel_result.confidence,
                state=kernel_result.state,
                retrieval_used=kernel_result.retrievalUsed,
                retrieval_chunk_count=kernel_result.retrievalChunkCount,
                decision=kernel_result.decision,
                tools_called=kernel_result.toolsCalled,
                response=kernel_result.reply,
                model=settings.llm_model,
                latency_ms=latency_ms,
                cost_usd=None,
                error=(
                    "kernel_fallback"
                    if kernel_result.errorOccurred
                    else None
                ),
                language_latency_ms=kernel_result.languageLatencyMs,
                intent_latency_ms=kernel_result.intentLatencyMs,
                context_latency_ms=kernel_result.contextLatencyMs,
                memory_latency_ms=kernel_result.memoryLatencyMs,
                rag_latency_ms=kernel_result.ragLatencyMs,
                llm_latency_ms=kernel_result.llmLatencyMs,
                input_tokens=kernel_result.inputTokens,
                output_tokens=kernel_result.outputTokens,
                error_category=kernel_result.errorCategory,
                trace_id=trace_id,
                channel=request.channel,
                status=kernel_result.status,
                failed_step=kernel_result.failedStep,
                steps=kernel_result.steps,
            ),
        )

        # ==============================================================
        # Language Experience Store
        # ==============================================================
        #
        # The language experience is only created when the Kernel
        # actually detected a language.
        #
        # experience_id is created at the beginning of the turn so it can
        # be shared by language, routing, novelty, cost and model records.
        #
        is_novel = False

        if kernel_result.detectedLanguage is not None:
            brain_used = brain_result is not None
            brain_agreement = None

            verification_level = "unverified"
            source_reliability = None

            if brain_used:
                brain_agreement = (
                    brain_result.predicted_intent
                    == kernel_result.intent
                )

                if (
                    brain_agreement
                    and routing_decision.agreement_rate is not None
                ):
                    verification_level = "self_consistent"
                    source_reliability = (
                        routing_decision.agreement_rate
                    )

                logger.info(
                    "Shadow brain prediction tenant=%s conv=%s "
                    "predicted=%s final_llm_intent=%s similarity=%.3f "
                    "agree=%s routed_to=%s",
                    request.tenantId,
                    request.conversationId,
                    brain_result.predicted_intent,
                    kernel_result.intent,
                    brain_result.similarity,
                    brain_agreement,
                    routing_decision.routed_to,
                )

            await record_language_experience(
                db,
                LanguageExperience(
                    experience_id=experience_id,
                    tenant_id=request.tenantId,
                    customer_id=request.customerId,
                    conversation_id=request.conversationId,
                    message_id=request.messageId,
                    request_id=request.requestId,
                    channel=request.channel,
                    original_message=request.message,
                    detected_language=kernel_result.detectedLanguage,
                    reply_language=kernel_result.replyLanguage,
                    normalized_message=kernel_result.normalizedMessage,
                    communication_style=kernel_result.communicationStyle,
                    is_ambiguous=kernel_result.isAmbiguous,
                    ambiguity_reason=kernel_result.ambiguityReason,
                    entity_spans=kernel_result.entitySpans,
                    brain_used=brain_used,
                    brain_prediction=(
                        {
                            "predicted_intent": (
                                brain_result.predicted_intent
                            ),
                            "similarity": brain_result.similarity,
                            "cluster_id": brain_result.cluster_id,
                            "matched_example": (
                                brain_result.matched_example
                            ),
                            "agreement": brain_agreement,
                            "routed_to": routing_decision.routed_to,
                            "calibrated_threshold": (
                                routing_decision.calibrated_threshold
                            ),
                        }
                        if brain_used
                        else None
                    ),
                    brain_version=(
                        SHADOW_BRAIN_VERSION
                        if brain_used
                        else None
                    ),
                    llm_raw_confidence=kernel_result.languageConfidence,
                    llm_model_version=settings.llm_model,
                    final_intent=kernel_result.intent,
                    learning_eligible=(
                        control_plane.is_language_learning_eligible(
                            kernel_result.intent
                        )
                    ),
                    verification_level=verification_level,
                    source_reliability=source_reliability,
                    script=getattr(
                        kernel_result,
                        "script",
                        "und",
                    ),
                    is_transliterated=getattr(
                        kernel_result,
                        "isTransliterated",
                        False,
                    ),
                    transliterated_from=getattr(
                        kernel_result,
                        "transliteratedFrom",
                        None,
                    ),
                    code_mixing=getattr(
                        kernel_result,
                        "codeMixing",
                        "none",
                    ),
                    language=getattr(
                        kernel_result,
                        "language",
                        "und",
                    ),
                ),
            )

            # ==========================================================
            # Novelty Detection
            # ==========================================================
            #
            # Uses the already-computed brain result; no second brain
            # prediction is required.
            #
            if kernel_result.normalizedMessage:
                novelty = await novelty_detector.check(
                    db,
                    tenant_id=request.tenantId,
                    customer_id=request.customerId,
                    conversation_id=request.conversationId,
                    experience_id=experience_id,
                    request_id=request.requestId,
                    normalized_message=(
                        kernel_result.normalizedMessage
                    ),
                    brain_prediction=brain_result,
                )

                is_novel = bool(novelty.is_novel)

        # ==============================================================
        # Routing Decision Persistence
        # ==============================================================
        #
        # IMPORTANT:
        # This is deliberately OUTSIDE the language-detection gate.
        #
        # Routing is a routing concern, not a language-detection concern.
        # Therefore a routing record should not disappear merely because
        # the Kernel did not return detectedLanguage.
        #
        await _record_routing_decision(
            db,
            tenant_id=request.tenantId,
            customer_id=request.customerId,
            conversation_id=request.conversationId,
            message_id=request.messageId,
            request_id=request.requestId,
            experience_id=experience_id,
            decision=routing_decision,
            final_intent=kernel_result.intent,
        )

        # ==============================================================
        # Phase 4: Conversation Cost Tracking
        # ==============================================================
        #
        # Runs for every turn, including language fallback turns.
        #
        await conversation_cost_tracker.record(
            db,
            tenant_id=request.tenantId,
            conversation_id=request.conversationId,
            input_tokens=kernel_result.inputTokens,
            output_tokens=kernel_result.outputTokens,
            intent_source=kernel_result.intentSource,
            is_resolved=(
                kernel_result.state.get("resolved", False)
                if isinstance(kernel_result.state, dict)
                else False
            ),
        )

        # ==============================================================
        # Phase 5: GLB per-turn understanding
        # ==============================================================
        #
        # Read-only assembly. It computes no new model result and must
        # never affect the customer reply.
        #
        understanding = None

        try:
            understanding = glb.understanding_to_dict(
                glb.assemble_turn_understanding(
                    language=kernel_result.language,
                    script=kernel_result.script,
                    is_transliterated=(
                        kernel_result.isTransliterated
                    ),
                    code_mixing=kernel_result.codeMixing,
                    reply_language=kernel_result.replyLanguage,
                    normalized_message=(
                        kernel_result.normalizedMessage
                    ),
                    intent=kernel_result.intent,
                    intent_confidence=kernel_result.confidence,
                    intent_source=kernel_result.intentSource,
                    is_ambiguous=kernel_result.isAmbiguous,
                    is_novel=is_novel,
                )
            )
        except Exception as exc:
            logger.warning(
                "glb understanding assembly failed (ignored): %s",
                exc,
            )

        # ==============================================================
        # Phase 5: Advisory GLB plan
        # ==============================================================
        #
        # The plan is informational/advisory at this stage.
        # It does not change the Kernel reply.
        #
        plan = None

        if understanding is not None:
            try:
                plan = plan_to_dict(
                    plan_turn(understanding)
                )
            except Exception as exc:
                logger.warning(
                    "glb plan failed (ignored): %s",
                    exc,
                )

        # ==============================================================
        # Persist GLB understanding
        # ==============================================================
        if understanding is not None:
            await record_turn_understanding(
                db,
                tenant_id=request.tenantId,
                customer_id=request.customerId,
                conversation_id=request.conversationId,
                message_id=request.messageId,
                request_id=request.requestId,
                experience_id=experience_id,
                understanding=understanding,
                plan=plan,
            )

        # ==============================================================
        # Phase 6: Model Shadow
        # ==============================================================
        #
        # Optional, default OFF.
        # Records only; never changes the reply.
        #
        if settings.model_shadow_enabled:
            from app.ai.embedding_service import embedding_service

            await model_shadow.run_shadow(
                db,
                enabled=True,
                tenant_id=request.tenantId,
                conversation_id=request.conversationId,
                experience_id=experience_id,
                text=kernel_result.normalizedMessage,
                served_intent=kernel_result.intent,
                served_source=kernel_result.intentSource,
                language=kernel_result.language,
                embed_fn=embedding_service.embed_batch,
                embedding_model=embedding_service.model,
            )

        # ==============================================================
        # Additive model-serving metadata
        # ==============================================================
        #
        # When the feature is OFF, no model_serving metadata is added.
        # Therefore the existing response structure remains intact.
        #
        extra_metadata = {}

        if model_serve is not None:
            extra_metadata["model_serving"] = {
                "served": model_serve.serve,
                "reason": model_serve.reason,
                "version": model_serve.version,
                "status": model_serve.status,
                "audited": model_serve.audited,
                "automation_eligible": (
                    model_serve.automation_eligible
                ),
            }

        # ==============================================================
        # Stable AgentResponse
        # ==============================================================
        #
        # Preserve MASTER response fields:
        #   reply
        #   intent
        #   confidence
        #   state
        #   toolsCalled
        #   request.metadata
        #   language metadata
        #
        # Language Intelligence metadata is additive.
        #
        return AgentResponse(
            reply=kernel_result.reply,
            intent=kernel_result.intent,
            confidence=kernel_result.confidence,
            state=kernel_result.state,
            toolsCalled=kernel_result.toolsCalled,
            metadata={
                **request.metadata,
                **extra_metadata,
                "language": {
                    "detected": kernel_result.detectedLanguage,
                    "reply": kernel_result.replyLanguage,
                },
                "understanding": understanding,
                "plan": plan,
                "routing": {
                    "routed_to": routing_decision.routed_to,
                    "predicted_intent": (
                        routing_decision.predicted_intent
                    ),
                    "similarity": routing_decision.similarity,
                    "calibrated_threshold": (
                        routing_decision.calibrated_threshold
                    ),
                    "intent_source": kernel_result.intentSource,
                },
            },
        )


core_agent = CoreAgent()

