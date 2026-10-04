"""
MODEL SHADOW RUNNER -- Phase 6 (docs/PHASE_5_8_PLAN.md P6-2).

Scores a live turn with the tenant's own model that is in status
'shadow' and RECORDS what it would have predicted next to what the
served path decided. It never changes the reply, the intent, or routing.

    served path (brain/LLM) ---> reply (unchanged)
             \\--> [after reply] model_shadow.run_shadow(...) --> table

Isolation: run_shadow() never raises; disabled by default
(settings.model_shadow_enabled). One extra embedding call per shadowed
turn -- the reason it is a flag. The loaded model is cached per tenant
for CACHE_TTL_SECONDS so a turn does not read the artifact from Postgres
each time; a stale cache can only delay a status change by that long.

The evidence this produces (agreement with the served intent, coverage at
the answer threshold, per-language slices) is what P6-3 (model canary)
must read before any traffic is given to a model. `summarize()` and
`ready_for_canary()` are the pure readers; thresholds are uncalibrated
placeholders (THRESHOLDS_CALIBRATED = False).

Agreement is against the SERVED intent, which is itself an LLM/brain
label, not ground truth -- so it bounds how good the model is, it does
not prove it. Ground truth needs human/outcome verification (§2).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import numpy as np

from app.core.logging import logger
from app.language.intent_model import IntentModel, ModelError

THRESHOLDS_CALIBRATED = False
CACHE_TTL_SECONDS = 300
RETENTION_DAYS = 90
MIN_SHADOW_SAMPLES = 200
MIN_AGREEMENT = 0.90
ANSWER_THRESHOLD = 0.85         # same meaning as model_eval.ANSWER_THRESHOLD
MIN_LANG_SLICE_N = 20
MIN_LANG_AGREEMENT = 0.80

_cache: dict[int, tuple[float, tuple[str, IntentModel, str] | None]] = {}


def clear_cache() -> None:
    _cache.clear()


async def load_shadow_model(db, tenant_id: int, *, now=time.monotonic):
    """(version, model, embedding_model) for the tenant's newest 'shadow'
    model, or None. Cached; None is cached too (no model = cheap turns)."""
    from sqlalchemy import text  # lazy

    hit = _cache.get(tenant_id)
    if hit and now() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    row = (await db.execute(
        text("""SELECT version, artifact, embedding_model FROM language_models
                 WHERE tenant_id = :t AND capability = 'intent' AND status = 'shadow'
                 ORDER BY created_at DESC LIMIT 1"""),
        {"t": tenant_id},
    )).first()
    loaded = None
    if row is not None:
        art = row.artifact if isinstance(row.artifact, dict) else json.loads(row.artifact)
        loaded = (row.version, IntentModel.from_dict(art), row.embedding_model)
    _cache[tenant_id] = (now(), loaded)
    return loaded


@dataclass(frozen=True)
class ShadowPrediction:
    model_version: str
    predicted_intent: str
    confidence: float
    served_intent: str | None
    served_source: str | None
    agrees: bool | None


def score(model: IntentModel, version: str, vector, served_intent: str | None,
          served_source: str | None) -> ShadowPrediction:
    """Pure. Raises ModelError on a wrong-dimension vector (caller isolates)."""
    (label, conf), = model.predict(np.asarray(vector, dtype=float)[None, :])
    return ShadowPrediction(
        version, label, conf, served_intent, served_source,
        None if served_intent is None else (label == served_intent),
    )


async def run_shadow(
    db, *, enabled: bool, tenant_id: int, conversation_id: int,
    experience_id, text: str | None, served_intent: str | None,
    served_source: str | None, language: str | None, embed_fn,
    embedding_model: str,
) -> ShadowPrediction | None:
    """Never raises. Returns the prediction when one was recorded."""
    if not enabled or not (text or "").strip():
        return None
    try:
        loaded = await load_shadow_model(db, tenant_id)
        if loaded is None:
            return None
        version, model, trained_with = loaded
        if trained_with != embedding_model:
            # weights are only valid for the embedding model they were trained on
            logger.warning("model_shadow skipped: model %s trained with %s, live is %s",
                           version, trained_with, embedding_model)
            return None
        vector = (await embed_fn([text]))[0]
        pred = score(model, version, vector, served_intent, served_source)
        from sqlalchemy import text as sql  # lazy
        await db.execute(
            sql("""INSERT INTO model_shadow_predictions
                     (tenant_id, conversation_id, experience_id, model_version,
                      predicted_intent, confidence, served_intent, served_source,
                      agrees, language)
                   VALUES (:t, :c, :e, :v, :pi, :conf, :si, :ss, :ag, :lang)"""),
            {"t": tenant_id, "c": conversation_id,
             "e": str(experience_id) if experience_id else None, "v": pred.model_version,
             "pi": pred.predicted_intent, "conf": pred.confidence, "si": served_intent,
             "ss": served_source, "ag": pred.agrees, "lang": (language or "und")},
        )
        await db.commit()
        return pred
    except Exception as exc:  # noqa: BLE001 -- must never touch the reply
        logger.warning("model_shadow failed (ignored): %s", exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


# ── pure readers ────────────────────────────────────────────────────

def summarize(rows: list[dict]) -> dict:
    """rows: dicts with confidence, agrees (bool|None), language.
    Counts/shares only. Turns with unknown served intent are excluded
    from agreement (reported as `unscored`), never counted as either."""
    scored = [r for r in rows if r.get("agrees") is not None]
    n = len(scored)
    agree = sum(1 for r in scored if r["agrees"])
    answered = [r for r in scored if r["confidence"] >= ANSWER_THRESHOLD]
    langs: dict[str, dict] = {}
    for r in scored:
        d = langs.setdefault(r.get("language") or "und", {"n": 0, "agree": 0})
        d["n"] += 1
        d["agree"] += 1 if r["agrees"] else 0
    return {
        "total": len(rows), "scored": n, "unscored": len(rows) - n,
        "agreement": (agree / n) if n else None,
        "coverage": (len(answered) / n) if n else None,
        "answered_agreement": (sum(1 for r in answered if r["agrees"]) / len(answered)) if answered else None,
        "per_language": {k: {"n": v["n"], "agreement": v["agree"] / v["n"]} for k, v in sorted(langs.items())},
        "thresholds_calibrated": THRESHOLDS_CALIBRATED,
    }


def ready_for_canary(summary: dict) -> list[str]:
    """Empty list == shadow evidence is sufficient to START a canary
    (not to serve). Each string names one unmet rule."""
    f: list[str] = []
    if summary["scored"] < MIN_SHADOW_SAMPLES:
        return [f"too_few_samples:{summary['scored']}<{MIN_SHADOW_SAMPLES}"]
    if summary["agreement"] < MIN_AGREEMENT:
        f.append(f"agreement:{summary['agreement']:.3f}<{MIN_AGREEMENT}")
    if summary["answered_agreement"] is not None and summary["answered_agreement"] < 0.95:
        f.append(f"answered_agreement:{summary['answered_agreement']:.3f}<0.95")
    for lg, s in summary["per_language"].items():
        if s["n"] >= MIN_LANG_SLICE_N and s["agreement"] < MIN_LANG_AGREEMENT:
            f.append(f"language_slice:{lg} {s['agreement']:.3f}<{MIN_LANG_AGREEMENT}")
    return f


SUMMARY_SQL = """
    SELECT confidence, agrees, language
      FROM model_shadow_predictions
     WHERE tenant_id = :t AND model_version = :v
       AND created_at >= CURRENT_TIMESTAMP - make_interval(days => :days)
"""
PURGE_SQL = "DELETE FROM model_shadow_predictions WHERE expires_at < CURRENT_TIMESTAMP"


async def purge_expired(db) -> int:
    """Delete predictions past their 90-day retention. Returns rows deleted.
    Companion to purge jobs already in deploy/crontab.example (P6-2b)."""
    res = await db.execute(_import_text()(PURGE_SQL))
    await db.commit()
    return getattr(res, "rowcount", 0) or 0


def _import_text():
    from sqlalchemy import text
    return text


async def report_for(db, tenant_id: int, version: str, days: int = 30) -> dict:
    from sqlalchemy import text  # lazy
    days = max(1, min(int(days), RETENTION_DAYS))
    res = await db.execute(text(SUMMARY_SQL), {"t": tenant_id, "v": version, "days": days})
    rows = [{"confidence": r.confidence, "agrees": r.agrees, "language": r.language} for r in res]
    s = summarize(rows)
    s["unmet_for_canary"] = ready_for_canary(s) if s["scored"] else ["no_data"]
    return s


async def _main(args) -> int:  # pragma: no cover -- needs real DB
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        if args.purge:
            n = await purge_expired(db)
            print(f"purged {n} expired model_shadow_predictions rows")
            return 0
        if args.tenant and args.version:
            print(__import__("json").dumps(await report_for(db, args.tenant, args.version, days=args.days),
                                            indent=2, default=str))
            return 0
    print("nothing to do: pass --purge, or --tenant + --version for a report")
    return 1


if __name__ == "__main__":  # pragma: no cover
    import argparse, asyncio as _asyncio, sys as _sys
    ap = argparse.ArgumentParser(description="model_shadow.py: --purge expired rows, or report for a model")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--tenant", type=int)
    ap.add_argument("--version")
    ap.add_argument("--days", type=int, default=30)
    _sys.exit(_asyncio.run(_main(ap.parse_args())))
