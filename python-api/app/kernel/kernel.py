import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ai_service import ai_service
from app.context.context_engine import AssembledContext, context_engine
from app.core.config import settings
from app.core.error_types import classify_exception
from app.core.logging import logger
from app.intent.intent_engine import intent_engine
from app.intent.intent_types import IntentType
from app.language.language_engine import language_engine
from app.memory.memory_service import memory_service
from app.schemas.kernel import KernelRunResponse
from app.state.state_engine import state_engine

RESPONSE_SYSTEM_PROMPT = """You are a helpful, concise customer support agent \
for this business. Answer using ONLY the provided memory, conversation state, \
and knowledge context. If the knowledge context doesn't contain the answer, \
say so honestly instead of guessing. Never invent prices, stock, or order \
details that are not in the context."""

CLARIFICATION_SYSTEM_PROMPT = """You are a helpful customer support agent. \
You are not confident what the customer is asking for. Ask ONE short, \
friendly clarifying question."""

# Shown to the customer only if the ENTIRE Kernel run fails unexpectedly
# (e.g. the LLM provider is down). Never leaks exception details.
FALLBACK_REPLY = (
    "দুঃখিত, এই মুহূর্তে সাড়া দিতে একটু সমস্যা হচ্ছে। একটু পরে আবার চেষ্টা করুন।"
)


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


class Kernel:
    """
    The reusable orchestration layer (architecture doc sections 21-23).

    Lifecycle for this v1 (Language Engine + Intent Engine + State
    Engine implemented; Tools/Business Rules/Negotiation are the next
    layers to add -- see section 40, do not skip ahead):

        Receive -> Understand Language -> Understand Intent ->
        Remember/Retrieve (Context: Memory + RAG + State) ->
        Reason (LLM) -> Decide (confidence gate) -> Respond ->
        Memory Update (Learn)

    Error handling (section 20, controlled failure): a failure in RAG
    retrieval degrades to an empty knowledge context rather than
    failing the request; a failure in Memory Extraction never blocks
    the reply that's already been generated; a failure ANYWHERE ELSE
    (Language/Intent/LLM) returns a safe fallback reply instead of
    raising an unhandled exception up to Node.js/the customer.

    Phase 1 observability (docs/OBSERVABILITY.md): every step below is
    individually timed (language/intent/context/llm), matching the new
    agent_run_traces columns from db/init/007_observability.sql. This
    is timing ONLY -- none of the control flow, prompts, or error
    boundaries described above changed to add it.

    Kernel v1 is intentionally a plain application layer inside
    Python, not a separate microservice (section 21).
    """

    async def run(
        self,
        db: AsyncSession,
        tenant_id: int,
        customer_id: int,
        conversation_id: int,
        message: str,
        request_id: str | None = None,
    ) -> KernelRunResponse:
        kernel_started_at = time.perf_counter()

        try:
            # 1. UNDERSTAND LANGUAGE
            language_started_at = time.perf_counter()
            language_result = await language_engine.understand(message)
            language_latency_ms = _elapsed_ms(language_started_at)
            logger.info(
                "Kernel language request_id=%s tenant=%s conv=%s detected=%s reply=%s confidence=%.2f latency_ms=%d",
                request_id,
                tenant_id,
                conversation_id,
                language_result.detected_language,
                language_result.reply_language,
                language_result.confidence,
                language_latency_ms,
            )
            # New structured signals (this increment) -- logged only for
            # now. communication_style/is_ambiguous are not yet consumed
            # by any behavior change (no reply-tone adjustment, no
            # confidence-gate change) -- that's future scope, not this
            # increment. entity_spans IS consumed, one line below, as a
            # hint to the Intent Engine.
            logger.info(
                "Kernel language-structure request_id=%s tenant=%s conv=%s "
                "style=%s ambiguous=%s reason=%s spans=%s",
                request_id,
                tenant_id,
                conversation_id,
                language_result.communication_style,
                language_result.is_ambiguous,
                language_result.ambiguity_reason,
                language_result.entity_spans,
            )

            # 2. UNDERSTAND INTENT
            intent_started_at = time.perf_counter()
            intent_result = await intent_engine.classify(
                language_result.normalized_message,
                entity_hints=language_result.entity_spans,
            )
            intent_latency_ms = _elapsed_ms(intent_started_at)
            logger.info(
                "Kernel intent request_id=%s tenant=%s conv=%s intent=%s confidence=%.2f latency_ms=%d",
                request_id,
                tenant_id,
                conversation_id,
                intent_result.intent.value,
                intent_result.confidence,
                intent_latency_ms,
            )

            # 3. STATE
            state = await state_engine.update_state(
                db, tenant_id, conversation_id, intent_result
            )

            reply_language_instruction = (
                f"\n\nReply in this language: {language_result.reply_language}."
            )

            # 4. DECIDE (confidence gate, section 18)
            if intent_result.confidence < settings.intent_confidence_min:
                llm_started_at = time.perf_counter()
                reply = await ai_service.complete(
                    message,
                    system=CLARIFICATION_SYSTEM_PROMPT + reply_language_instruction,
                )
                llm_latency_ms = _elapsed_ms(llm_started_at)
                response = KernelRunResponse(
                    reply=reply,
                    intent=intent_result.intent.value,
                    confidence=intent_result.confidence,
                    state=state,
                    toolsCalled=[],
                    detectedLanguage=language_result.detected_language,
                    replyLanguage=language_result.reply_language,
                    normalizedMessage=language_result.normalized_message,
                    decision="clarify",
                    retrievalUsed=False,
                    retrievalChunkCount=0,
                    languageLatencyMs=language_latency_ms,
                    intentLatencyMs=intent_latency_ms,
                    llmLatencyMs=llm_latency_ms,
                )
            else:
                # 5. RETRIEVE / CONTEXT -- isolated failure boundary. RAG
                # failures are now handled INSIDE context_engine.assemble()
                # itself (memories are never discarded just because
                # knowledge search fails). This try/except remains as a
                # safety net for anything else that could go wrong here
                # (e.g. a Memory/DB error) -- if it still fails, fall back
                # to an empty context entirely rather than failing the
                # whole request.
                context_started_at = time.perf_counter()
                try:
                    context = await context_engine.assemble(
                        db,
                        tenant_id,
                        customer_id,
                        question=message,
                        state=state,
                        retrieval_query=language_result.normalized_message,
                    )
                except Exception:
                    logger.error(
                        "Context assembly failed request_id=%s tenant=%s conv=%s -- "
                        "continuing with empty context",
                        request_id,
                        tenant_id,
                        conversation_id,
                        exc_info=True,
                    )
                    context = AssembledContext(question=message, state=state)
                context_latency_ms = _elapsed_ms(context_started_at)

                # 6. REASON
                tools_called: list[str] = []
                if intent_result.intent in (
                    IntentType.CREATE_ORDER,
                    IntentType.ORDER_STATUS,
                ):
                    logger.info(
                        "Intent %s requires a Tool that is not yet implemented; "
                        "falling back to a grounded LLM answer.",
                        intent_result.intent.value,
                    )

                llm_started_at = time.perf_counter()
                reply = await ai_service.complete(
                    context.to_prompt_block(),
                    system=RESPONSE_SYSTEM_PROMPT + reply_language_instruction,
                )
                llm_latency_ms = _elapsed_ms(llm_started_at)

                response = KernelRunResponse(
                    reply=reply,
                    intent=intent_result.intent.value,
                    confidence=intent_result.confidence,
                    state=state,
                    toolsCalled=tools_called,
                    detectedLanguage=language_result.detected_language,
                    replyLanguage=language_result.reply_language,
                    normalizedMessage=language_result.normalized_message,
                    decision="answered",
                    retrievalUsed=len(context.knowledge_chunks) > 0,
                    retrievalChunkCount=len(context.knowledge_chunks),
                    languageLatencyMs=language_latency_ms,
                    intentLatencyMs=intent_latency_ms,
                    contextLatencyMs=context_latency_ms,
                    memoryLatencyMs=context.memory_latency_ms,
                    ragLatencyMs=context.rag_latency_ms,
                    llmLatencyMs=llm_latency_ms,
                )

            # 7. LEARN -- isolated failure boundary. Runs after the reply
            # is already built, and its own failure never affects what's
            # returned to the customer.
            try:
                await memory_service.extract_and_store(
                    db, tenant_id, customer_id, message
                )
            except Exception:
                logger.error(
                    "Memory extraction failed request_id=%s tenant=%s customer=%s",
                    request_id,
                    tenant_id,
                    customer_id,
                    exc_info=True,
                )

            response.kernelLatencyMs = _elapsed_ms(kernel_started_at)
            logger.info(
                "Kernel run completed request_id=%s tenant=%s conv=%s latency_ms=%d",
                request_id,
                tenant_id,
                conversation_id,
                response.kernelLatencyMs,
            )
            return response

        except Exception as exc:
            kernel_latency_ms = _elapsed_ms(kernel_started_at)
            error_category = classify_exception(exc)
            logger.error(
                "Kernel run FAILED request_id=%s tenant=%s conv=%s latency_ms=%d "
                "error_category=%s -- returning fallback reply",
                request_id,
                tenant_id,
                conversation_id,
                kernel_latency_ms,
                error_category.value,
                exc_info=True,
            )
            return KernelRunResponse(
                reply=FALLBACK_REPLY,
                intent=None,
                confidence=0.0,
                state={},
                toolsCalled=[],
                decision="fallback",
                errorOccurred=True,
                kernelLatencyMs=kernel_latency_ms,
                errorCategory=error_category.value,
            )


kernel = Kernel()
