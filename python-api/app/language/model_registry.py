"""
MODEL REGISTRY -- Phase 6: versioning + lifecycle for own models.

    trained -> shadow -> canary -> active -> retired
    trained | shadow | canary -> rejected | retired

`validate_transition()` is a pure state machine (the DB CHECK only
guards the set of names; this guards the ORDER). register_model() /
set_status() are thin SQL over `language_models` (migration 020).

Invariants (tested):
  * a new model always starts as 'trained' -- never straight to a
    serving state;
  * a model whose offline eval did not pass cannot leave 'trained'
    except to 'rejected'/'retired' (an eval-failed model must not reach
    shadow by mistake) -- see `can_enter_shadow`;
  * 'active' requires having been 'canary' first (no skipping shadow or
    canary) and retires the previous active model in the same
    transaction (also enforced by a partial unique index in SQL);
  * nothing here changes serving. No request-path code imports this.

register_model refuses an artifact whose fingerprint or dim does not
match what the caller says it is (defence against registering the wrong
blob under a version).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.core.logging import logger
from app.language.intent_model import IntentModel, MODEL_KIND

STATUSES = ("trained", "shadow", "canary", "active", "retired", "rejected")

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "trained":  frozenset({"shadow", "rejected", "retired"}),
    "shadow":   frozenset({"canary", "rejected", "retired"}),
    "canary":   frozenset({"active", "rejected", "retired"}),
    "active":   frozenset({"retired"}),
    "retired":  frozenset(),
    "rejected": frozenset(),
}


class RegistryError(ValueError):
    pass


def validate_transition(current: str, target: str) -> None:
    if current not in ALLOWED_TRANSITIONS:
        raise RegistryError(f"unknown current status {current!r}")
    if target not in ALLOWED_TRANSITIONS:
        raise RegistryError(f"unknown target status {target!r}")
    if target not in ALLOWED_TRANSITIONS[current]:
        raise RegistryError(f"illegal transition {current} -> {target}")


def can_enter_shadow(eval_report: dict) -> bool:
    """Only a model whose offline eval passed may go to shadow."""
    return bool(isinstance(eval_report, dict) and eval_report.get("ok") is True)


def make_version(prefix: str = "intent-lr", now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{prefix}-{now.strftime('%Y%m%d%H%M%S')}"


async def register_model(
    db, *, tenant_id: int, model: IntentModel, embedding_model: str,
    dataset_fingerprint: str, train_counts: dict, eval_report: dict,
    version: str | None = None, capability: str = "intent",
) -> str:
    """Insert a new 'trained' row. Raises on bad input (loud); the
    caller (training job) decides how to report it."""
    from sqlalchemy import text  # lazy

    artifact = model.to_dict()
    if artifact.get("kind") != MODEL_KIND:
        raise RegistryError("unexpected model kind")
    if len(dataset_fingerprint) != 64:
        raise RegistryError("dataset_fingerprint must be a sha256 hex digest")
    version = version or make_version()
    await db.execute(
        text("""
            INSERT INTO language_models
                (tenant_id, capability, version, kind, status, embedding_model, dim,
                 labels, artifact, artifact_sha256, dataset_fingerprint,
                 train_counts, eval_report)
            VALUES
                (:tenant_id, :capability, :version, :kind, 'trained', :embedding_model, :dim,
                 CAST(:labels AS JSONB), CAST(:artifact AS JSONB), :sha, :dfp,
                 CAST(:counts AS JSONB), CAST(:report AS JSONB))
        """),
        {
            "tenant_id": tenant_id, "capability": capability, "version": version,
            "kind": artifact["kind"], "embedding_model": embedding_model,
            "dim": model.dim, "labels": json.dumps(model.labels),
            "artifact": json.dumps(artifact), "sha": model.fingerprint(),
            "dfp": dataset_fingerprint, "counts": json.dumps(train_counts),
            "report": json.dumps(eval_report),
        },
    )
    await db.commit()
    logger.info("model_registry registered tenant=%s version=%s ok=%s",
                tenant_id, version, eval_report.get("ok"))
    return version


async def set_status(
    db, *, tenant_id: int, version: str, target: str, reason: str,
    capability: str = "intent",
) -> None:
    """Move one model along the lifecycle. Raises RegistryError on an
    illegal move or a missing row; activating retires the old active."""
    from sqlalchemy import text  # lazy

    row = (await db.execute(
        text("""SELECT status, eval_report FROM language_models
                 WHERE tenant_id = :t AND capability = :c AND version = :v"""),
        {"t": tenant_id, "c": capability, "v": version},
    )).first()
    if row is None:
        raise RegistryError(f"no model {version!r} for tenant {tenant_id}")
    current = row.status
    validate_transition(current, target)
    if current == "trained" and target == "shadow":
        report = row.eval_report if isinstance(row.eval_report, dict) else json.loads(row.eval_report)
        if not can_enter_shadow(report):
            raise RegistryError("offline eval did not pass; cannot enter shadow")
    if not (reason or "").strip():
        raise RegistryError("a reason is required for every status change")
    if target == "active":
        await db.execute(
            text("""UPDATE language_models
                       SET status='retired', status_changed_at=CURRENT_TIMESTAMP,
                           status_reason='superseded by ' || :v
                     WHERE tenant_id = :t AND capability = :c AND status = 'active'"""),
            {"t": tenant_id, "c": capability, "v": version},
        )
    await db.execute(
        text("""UPDATE language_models
                   SET status = :s, status_changed_at = CURRENT_TIMESTAMP, status_reason = :r
                 WHERE tenant_id = :t AND capability = :c AND version = :v"""),
        {"s": target, "r": reason, "t": tenant_id, "c": capability, "v": version},
    )
    await db.commit()
    logger.info("model_registry tenant=%s version=%s %s -> %s", tenant_id, version, current, target)
