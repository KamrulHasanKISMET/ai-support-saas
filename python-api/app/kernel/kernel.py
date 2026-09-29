import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ai_service import ai_service
from app.context.context_engine import AssembledContext, context_engine
from app.core.config import settings
from app.core.error_types import classify_exception
from app.core.logging import logger
from app.core.trace_sanitize import safe_error_message
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


def _accumulate_token_usage(
    totals: dict[str, int | None], usage: dict[str, int | None] | None
) -> None:
    if not usage:
        return
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if value is None:
            continue
        current = totals.get(key)
        totals[key] = value if current is None else current + value

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
    individually timed (language/intent/context/llm), matching the
    agent_run_traces columns from db/init/007_observability.sql. This
    is timing ONLY -- none of the control flow, prompts, or error
    boundaries described above changed to add it.

    Commercial V1 trace lifecycle (db/init/008_trace_lifecycle.sql):
    additionally builds an ordered `steps` list (status + duration +
    safe error per lifecycle stage) and an overall `status`/`failedStep`
    -- again purely additive bookkeeping around the SAME calls above,
    not a control-flow change. See docs/OBSERVABILITY.md.

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
        steps: list[dict] = []
        last_step_attempted = "language"
        _token_totals: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
        }

        def _accumulate_usage(
            usage: dict[str, int | None] | None,
        ) -> None:
            _accumulate_token_usage(_token_totals, usage)

        def _step(name, status, duration_ms=None, error=None, metadata=None):
            entry: dict = {"step": name, "status": status}
            if duration_ms is not None:
                entry["durationMs"] = duration_ms
            if error:
                entry["error"] = error
            if metadata:
                entry["metadata"] = metadata
            steps.append(entry)

        try:
            # 1. UNDERSTAND LANGUAGE
            language_started_at = time.perf_counter()
            language_usage: dict[str, int | None] = {}
            language_result = await language_engine.understand(
              message,
              usage_out=language_usage,
            )
            _accumulate_usage(language_usage)
            language_latency_ms = _elapsed_ms(language_started_at)
            _step("language", "ok", language_latency_ms)
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
            # 2. UNDERSTAND INTENT
            last_step_attempted = "intent"
            intent_started_at = time.perf_counter()
            intent_usage: dict[str, int | None] = {}
            intent_result = await intent_engine.classify(
                language_result.normalized_message,
                entity_hints=language_result.entity_spans,
                usage_out=intent_usage,
            )
            _accumulate_usage(intent_usage)
            intent_latency_ms = _elapsed_ms(intent_started_at)

            _step("intent", "ok", intent_latency_ms)
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
            last_step_attempted = "state"
            state = await state_engine.update_state(
                db, tenant_id, conversation_id, intent_result
            )

            reply_language_instruction = (
                f"\n\nReply in this language: {language_result.reply_language}."
            )

            run_status = "completed"

            # 4. DECIDE (confidence gate, section 18)
            if intent_result.confidence < settings.intent_confidence_min:
                last_step_attempted = "llm"
                llm_started_at = time.perf_counter()
                clarification_usage: dict[str, int | None] = {}
                reply = await ai_service.complete(
                    message,
                    system=CLARIFICATION_SYSTEM_PROMPT + reply_language_instruction,
                    usage_out=clarification_usage,
                )
                _accumulate_usage(clarification_usage)

                llm_latency_ms = _elapsed_ms(llm_started_at)
                _step("llm", "ok", llm_latency_ms, metadata={"path": "clarify"})
                response = KernelRunResponse(
                    reply=reply,
                    intent=intent_result.intent.value,
                    inputTokens=_token_totals["input_tokens"],
                    outputTokens=_token_totals["output_tokens"],
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
                    status=run_status,
                )
            else:
                # 5. RETRIEVE / CONTEXT -- isolated failure boundary. RAG
                # and Memory failures are now handled INSIDE
                # context_engine.assemble() itself (see its own
                # docstring) -- this try/except remains as a safety net
                # for anything else that could go wrong at this call
                # boundary (e.g. an unexpected error in budgeting/
                # validation) -- if it still fails, fall back to an
                # empty context entirely rather than failing the whole
                # request.
                last_step_attempted = "context"
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
                    context_latency_ms = _elapsed_ms(context_started_at)
                    _step(
                        "context",
                        "ok",
                        context_latency_ms,
                        metadata={
                            "budget": context.budget_info,
                            "compressionApplied": context.compression_applied,
                            "validationErrors": context.validation_errors,
                        },
                    )
                except Exception as exc:
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
                    _step("context", "error", context_latency_ms, error=safe_error_message(exc))
                    run_status = "partial"

                # Memory/RAG sub-steps -- status comes from
                # ContextEngine (see its docstring); a "error" here
                # degrades the run to "partial", never to a hard
                # failure, since an answer was still produced.
                _step(
                    "memory",
                    context.memory_status,
                    context.memory_latency_ms,
                    error=context.memory_error,
                )
                _step(
                    "rag",
                    context.rag_status,
                    context.rag_latency_ms,
                    error=context.rag_error,
                )
                if context.memory_status == "error" or context.rag_status == "error":
                    run_status = "partial" if run_status == "completed" else run_status

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
                    _step(
                        "tool",
                        "skipped",
                        metadata={
                            "reason": "tool_engine_not_implemented",
                            "intent": intent_result.intent.value,
                        },
                    )

                last_step_attempted = "llm"
                llm_started_at = time.perf_counter()
                normal_reply_usage: dict[str, int | None] = {}
                reply = await ai_service.complete(
                    context.to_prompt_block(),
                    system=RESPONSE_SYSTEM_PROMPT + reply_language_instruction,
                    usage_out=normal_reply_usage,
                )
                _accumulate_usage(normal_reply_usage)
                llm_latency_ms = _elapsed_ms(llm_started_at)
                _step("llm", "ok", llm_latency_ms)

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
                    status=run_status,
                )

            # 7. LEARN -- isolated failure boundary. Runs after the reply
            # is already built, and its own failure never affects what's
            # returned to the customer or downgrades run_status -- a
            # background memory-write hiccup is not the same thing as
            # failing to serve the customer a good answer.
            memory_update_started_at = time.perf_counter()
            try:
                memory_usage: dict[str, int | None] = {}
                await memory_service.extract_and_store(
                    db,
                    tenant_id,
                    customer_id,
                    message,
                    usage_out=memory_usage,
                )
                _accumulate_usage(memory_usage)
                memory_update_latency_ms = _elapsed_ms(memory_update_started_at)
                _step("memory_update", "ok", memory_update_latency_ms)
            except Exception as exc:
                memory_update_latency_ms = _elapsed_ms(memory_update_started_at)
                _step(
                    "memory_update",
                    "error",
                    memory_update_latency_ms,
                    error=safe_error_message(exc),
                )
                logger.error(
                    "Memory extraction failed request_id=%s tenant=%s customer=%s",
                    request_id,
                    tenant_id,
                    customer_id,
                    exc_info=True,
                )

            response.kernelLatencyMs = _elapsed_ms(kernel_started_at)
            response.inputTokens = _token_totals["input_tokens"]
            response.outputTokens = _token_totals["output_tokens"]
            response.steps = steps
            response.status = run_status
            logger.info(
                "Kernel run completed request_id=%s tenant=%s conv=%s latency_ms=%d status=%s",
                request_id,
                tenant_id,
                conversation_id,
                response.kernelLatencyMs,
                run_status,
            )
            return response

        except Exception as exc:
            kernel_latency_ms = _elapsed_ms(kernel_started_at)
            error_category = classify_exception(exc)
            logger.error(
                "Kernel run FAILED request_id=%s tenant=%s conv=%s latency_ms=%d "
                "error_category=%s failed_step=%s -- returning fallback reply",
                request_id,
                tenant_id,
                conversation_id,
                kernel_latency_ms,
                error_category.value,
                last_step_attempted,
                inputTokens=_token_totals["input_tokens"],
                outputTokens=_token_totals["output_tokens"],
                exc_info=True,
            )
            steps.append(
                {
                    "step": last_step_attempted,
                    "status": "error",
                    "error": safe_error_message(exc),
                }
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
                status="error",
                failedStep=last_step_attempted,
                steps=steps,
            )


kernel = Kernel()
