"""
MODEL SERVING HOOK -- Phase 6 P6-4 (docs/PHASE_5_8_PLAN.md).

Lets a tenant's OWN intent model supply the intent for a turn, so the
Kernel can skip its LLM Intent Engine call -- under strict conditions,
OFF by default, with the LLM always the fallback.

    core_agent --> [flag ON?] --> model_serving.decide(...) --> ServeDecision
                                        |
                       serve + use_hint -+--> Kernel(predicted_intent_hint=intent)
                       otherwise         +--> Kernel runs exactly as before

What this IS
    * `decide()`  -- async, never raises, returns a ServeDecision or None.
    * `evaluate()` -- the PURE rule set (unit-tested, no DB/network).
    * `record_served()` -- isolated metadata-only write (migration 024).
    * `summarize_served()` / `report_for()` -- readers for the evidence.

Rules (every one must pass, checked in this order; the first failure is
returned as `reason`, so a "no" is always explainable):
    1. an eligible model exists: status 'active'; status 'canary' ONLY if
       the separate canary flag is on (default off);
    2. the model's own offline eval passed (`eval_report.ok is True`);
    3. the model was trained with the embedding model that is live now
       (weights are meaningless across embedding models);
    4. the model's calibrated confidence >= SERVE_MIN_CONFIDENCE;
    5. the predicted intent is a real IntentType (the Kernel would reject
       anything else and the turn would be mislabelled as "served");
    6. that intent has recorded evidence in the model's eval report AND
       its per-intent F1 >= MIN_PER_INTENT_F1 (an intent the model was
       never measured on is never served);
    7. for a canary model: this turn is inside the canary_pct sample.

Audit sampling (the honesty mechanism)
    When the model's intent is injected as the Kernel's hint, the
    Kernel's final intent equals the hint by construction, so
    "final == model" is always true and proves NOTHING about accuracy.
    So for AUDIT_SAMPLE_RATE of served turns `use_hint` is False: the LLM
    Intent Engine still runs, and `agrees` = (LLM intent == model intent)
    is real evidence. `agrees` is NULL on every non-audited row and is
    never inferred (the migration has a CHECK for it).

Action safety
    Serving an intent only supplies UNDERSTANDING. It never authorises
    an action: `automation_eligible` is recorded from
    control_plane.is_automation_eligible() so CREATE_ORDER/ORDER_STATUS
    stay flagged non-automatable for any future Tool Engine.

Known limitation (stated, not hidden)
    The hook runs BEFORE the Kernel, so it only has the RAW message;
    training used the NORMALIZED message (distillation_dataset.py). That
    input-distribution shift is exactly what the audit sample measures.
    (The shadow Brain in core_agent.py makes the same pre-Kernel choice.)

Thresholds are uncalibrated placeholders (THRESHOLDS_CALIBRATED=False).
Never lower one to make a model serve; calibrate from audited rows.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass

import numpy as np

from app.core.logging import logger
from app.intent.intent_types import IntentType
from app.language import control_plane
from app.language.intent_model import IntentModel

THRESHOLDS_CALIBRATED = False
SERVE_MIN_CONFIDENCE = 0.90       # >= model_eval.ANSWER_THRESHOLD (0.85) on purpose
MIN_PER_INTENT_F1 = 0.80
AUDIT_SAMPLE_RATE = 0.10          # share of served turns still classified by the LLM
CACHE_TTL_SECONDS = 60            # short: a retire/rollback must bite quickly
RETENTION_DAYS = 90

_VALID_INTENTS = frozenset(i.value for i in IntentType)

_cache: dict[int, tuple[float, "LoadedModel | None"]] = {}


def clear_cache() -> None:
    _cache.clear()


@dataclass(frozen=True)
class LoadedModel:
    version: str
    model: IntentModel
    embedding_model: str
    eval_report: dict
    status: str                     # 'active' | 'canary'
    canary_pct: float | None


@dataclass(frozen=True)
class ServeDecision:
    serve: bool                     # the model may supply this turn's intent
    reason: str                     # 'ok' or the first failed rule
    version: str
    status: str
    intent: str | None
    confidence: float | None
    use_hint: bool                  # inject as Kernel hint (False on audit turns)
    audited: bool
    canary_pct: float | None
    automation_eligible: bool


# ── pure rules ──────────────────────────────────────────────────────

def _no(reason: str, lm: LoadedModel, intent=None, conf=None) -> ServeDecision:
    return ServeDecision(
        serve=False, reason=reason, version=lm.version, status=lm.status,
        intent=intent, confidence=conf, use_hint=False, audited=False,
        canary_pct=lm.canary_pct,
        automation_eligible=control_plane.is_automation_eligible(intent),
    )


def evaluate(
    lm: LoadedModel, vector, *, embedding_model: str, canary_enabled: bool = False,
    rng=random.random, audit_rng=random.random,
) -> ServeDecision:
    """Pure. Raises IntentModel's ModelError on a wrong-dimension vector
    (the caller isolates it and falls back to the LLM)."""
    if lm.status == "canary" and not canary_enabled:
        return _no("canary_serving_disabled", lm)
    if lm.status not in ("active", "canary"):
        return _no("model_not_servable", lm)
    if not (isinstance(lm.eval_report, dict) and lm.eval_report.get("ok") is True):
        return _no("offline_eval_not_passed", lm)
    if lm.embedding_model != embedding_model:
        return _no("embedding_model_mismatch", lm)

    (label, conf), = lm.model.predict(np.asarray(vector, dtype=float)[None, :])

    if conf < SERVE_MIN_CONFIDENCE:
        return _no("low_confidence", lm, label, conf)
    if label not in _VALID_INTENTS:
        return _no("unknown_intent", lm, label, conf)
    f1s = lm.eval_report.get("per_intent_f1") or {}
    if label not in f1s:
        return _no("no_intent_evidence", lm, label, conf)
    if float(f1s[label]) < MIN_PER_INTENT_F1:
        return _no("intent_below_floor", lm, label, conf)
    if lm.status == "canary":
        if lm.canary_pct is None or rng() * 100 >= lm.canary_pct:
            return _no("canary_not_selected", lm, label, conf)

    audited = audit_rng() < AUDIT_SAMPLE_RATE
    return ServeDecision(
        serve=True, reason="ok", version=lm.version, status=lm.status,
        intent=label, confidence=conf, use_hint=not audited, audited=audited,
        canary_pct=lm.canary_pct,
        automation_eligible=control_plane.is_automation_eligible(label),
    )


# ── DB: load + decide + record ─────────────────────────────────────

_LOAD_SQL = """
    SELECT lm.version, lm.artifact, lm.embedding_model, lm.eval_report,
           lm.status, mcs.canary_pct
      FROM language_models lm
      LEFT JOIN model_canary_state mcs
        ON mcs.tenant_id = lm.tenant_id AND mcs.capability = lm.capability
       AND mcs.version = lm.version
     WHERE lm.tenant_id = :t AND lm.capability = 'intent'
       AND lm.status IN ('active', 'canary')
     ORDER BY (lm.status = 'active') DESC, lm.created_at DESC
     LIMIT 1
"""


def _as_dict(v):
    return v if isinstance(v, dict) else json.loads(v)


async def load_serving_model(db, tenant_id: int, *, now=time.monotonic) -> LoadedModel | None:
    """The tenant's active model (else canary). Cached, None cached too."""
    from sqlalchemy import text  # lazy

    hit = _cache.get(tenant_id)
    if hit and now() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    row = (await db.execute(text(_LOAD_SQL), {"t": tenant_id})).first()
    loaded = None
    if row is not None:
        loaded = LoadedModel(
            version=row.version, model=IntentModel.from_dict(_as_dict(row.artifact)),
            embedding_model=row.embedding_model, eval_report=_as_dict(row.eval_report),
            status=row.status,
            canary_pct=float(row.canary_pct) if row.canary_pct is not None else None,
        )
    _cache[tenant_id] = (now(), loaded)
    return loaded


async def decide(
    db, *, enabled: bool, canary_enabled: bool, tenant_id: int, text: str | None,
    embed_fn, embedding_model: str, rng=random.random, audit_rng=random.random,
) -> ServeDecision | None:
    """Never raises. None = hook off / no model / any failure => the
    caller behaves exactly as if this module did not exist."""
    if not enabled or not (text or "").strip():
        return None
    try:
        lm = await load_serving_model(db, tenant_id)
        if lm is None:
            return None
        vector = (await embed_fn([text]))[0]
        return evaluate(lm, vector, embedding_model=embedding_model,
                        canary_enabled=canary_enabled, rng=rng, audit_rng=audit_rng)
    except Exception as exc:  # noqa: BLE001 -- must never touch the reply
        logger.warning("model_serving decide failed (ignored, LLM answers): %s", exc)
        return None


async def record_served(
    db, *, tenant_id: int, conversation_id: int, experience_id, decision: ServeDecision,
    final_intent: str | None, kernel_used_hint: bool, language: str | None,
) -> bool:
    """Isolated, metadata-only. Only records turns the model was allowed
    to serve (decision.serve). Returns True if a row was written.

    `hint_used` is what the Kernel really did (`kernel_used_hint`), not
    what we asked for: a rejected hint is recorded as not used.
    `agrees` is set ONLY for audited turns (see module docstring)."""
    if decision is None or not decision.serve:
        return False
    try:
        from sqlalchemy import text as sql  # lazy
        agrees = (final_intent == decision.intent) if (decision.audited and final_intent) else None
        await db.execute(
            sql("""INSERT INTO model_served_turns
                     (tenant_id, conversation_id, experience_id, model_version, model_status,
                      canary_pct, model_intent, confidence, hint_used, audited, final_intent,
                      agrees, automation_eligible, language)
                   VALUES (:t, :c, :e, :v, :st, :pct, :mi, :conf, :hu, :au, :fi, :ag, :ae, :lang)"""),
            {"t": tenant_id, "c": conversation_id,
             "e": str(experience_id) if experience_id else None, "v": decision.version,
             "st": decision.status, "pct": decision.canary_pct, "mi": decision.intent,
             "conf": decision.confidence, "hu": bool(kernel_used_hint and not decision.audited),
             "au": decision.audited, "fi": final_intent, "ag": agrees,
             "ae": decision.automation_eligible, "lang": (language or "und")},
        )
        await db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_serving record failed (ignored): %s", exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


# ── readers (pure + thin SQL) ──────────────────────────────────────

def summarize_served(rows: list[dict]) -> dict:
    """rows: dicts with audited, agrees, hint_used, language.
    Accuracy is computed from AUDITED rows only; everything else is a
    count. No good/bad verdict is made here."""
    total = len(rows)
    audited = [r for r in rows if r.get("audited") and r.get("agrees") is not None]
    agree = sum(1 for r in audited if r["agrees"])
    langs: dict[str, dict] = {}
    for r in audited:
        d = langs.setdefault(r.get("language") or "und", {"n": 0, "agree": 0})
        d["n"] += 1
        d["agree"] += 1 if r["agrees"] else 0
    return {
        "served": total,
        "hint_used": sum(1 for r in rows if r.get("hint_used")),
        "audited": len(audited),
        "audited_agreement": (agree / len(audited)) if audited else None,
        "per_language": {k: {"n": v["n"], "agreement": v["agree"] / v["n"]}
                         for k, v in sorted(langs.items())},
        "thresholds_calibrated": THRESHOLDS_CALIBRATED,
        "note": "agreement comes from audited turns only (LLM still classified them)",
    }


SUMMARY_SQL = """
    SELECT audited, agrees, hint_used, language
      FROM model_served_turns
     WHERE tenant_id = :t AND model_version = :v
       AND created_at >= CURRENT_TIMESTAMP - make_interval(days => :days)
"""
PURGE_SQL = "DELETE FROM model_served_turns WHERE expires_at < CURRENT_TIMESTAMP"


async def purge_expired(db) -> int:
    from sqlalchemy import text  # lazy
    res = await db.execute(text(PURGE_SQL))
    await db.commit()
    return getattr(res, "rowcount", 0) or 0


async def report_for(db, tenant_id: int, version: str, days: int = 30) -> dict:
    from sqlalchemy import text  # lazy
    days = max(1, min(int(days), RETENTION_DAYS))
    res = await db.execute(text(SUMMARY_SQL), {"t": tenant_id, "v": version, "days": days})
    rows = [{"audited": r.audited, "agrees": r.agrees, "hint_used": r.hint_used,
             "language": r.language} for r in res]
    return summarize_served(rows)


async def _main(args) -> int:  # pragma: no cover -- needs real DB
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        if args.purge:
            print(f"purged {await purge_expired(db)} expired model_served_turns rows")
            return 0
        if args.tenant and args.version:
            print(json.dumps(await report_for(db, args.tenant, args.version, days=args.days),
                             indent=2, default=str))
            return 0
    print("nothing to do: pass --purge, or --tenant + --version for a report")
    return 1


if __name__ == "__main__":  # pragma: no cover
    import argparse, asyncio as _asyncio, sys as _sys
    ap = argparse.ArgumentParser(description="model_serving.py: --purge expired rows, or report for a model")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--tenant", type=int)
    ap.add_argument("--version")
    ap.add_argument("--days", type=int, default=30)
    _sys.exit(_asyncio.run(_main(ap.parse_args())))
