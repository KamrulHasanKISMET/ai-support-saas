"""
Admin: per-tenant calibration config (Phase 4 completion, step 1).

    GET /admin/tenants/{tenant_id}/calibration-config
    PUT /admin/tenants/{tenant_id}/calibration-config

Replaces manual SQL INSERT/UPDATE on `tenant_calibration_config`
(docs/LANGUAGE_INTELLIGENCE.md, "What the next session should build
first" item 1).

Protection model: identical to understanding.py / verification.py --
an internal operator tool gated by the node-api<->python-api internal
secret; there is no dashboard role layer yet (docs/ROADMAP.md §1 gap
applies). The tenant is named in the path and must exist.

PUT semantics (deliberate, read this):
    PARTIAL merge, not replace. Only the fields present in the body
    (and not null) change; everything else keeps its current value
    (or the code default when the tenant has no row yet). A JSON null
    is treated as "not provided" -- there is no way to "reset a field
    to default" through this route other than sending the default
    value explicitly. Unknown fields are rejected (extra="forbid"), so
    a typo cannot be silently ignored.

Why this route does NOT use TenantCalibrationConfig.load() for its
read: load() swallows DB errors and returns defaults (right for the
hot path, wrong here -- a partial merge on top of "defaults because the
DB hiccuped" would silently overwrite a tenant's real tuning). This
module reads strictly and lets a DB failure surface as a 500.

Validation is plain Python (merge_and_validate) rather than pydantic
constraints because the promote/rollback pair must be checked
against each other after merging with stored values.

The bounds below are POLICY CHOICES, not derived from data. Only the
0.70-0.99 agreement range comes from the migration's own comment
(014_phase4_scale_optimize.sql); the rest are conservative guard rails
chosen so an operator typo (e.g. 0.095 for 0.95) cannot disable the
safety gates. Change them here, in one place, with a reviewed commit.
"""

from dataclasses import MISSING, fields

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logging import logger
from app.core.security import require_internal_secret
from app.language.tenant_calibration_config import TenantCalibrationConfig

router = APIRouter(
    prefix="/admin",
    tags=["admin-calibration"],
    dependencies=[Depends(require_internal_secret)],
)

# ── Bounds (policy choices; see module docstring) ────────────────────

# field -> (min, max, kind) ; kind is "float" | "int"
BOUNDS: dict[str, tuple[float, float, str]] = {
    "min_agreement_rate": (0.70, 0.99, "float"),      # range from migration 014
    "min_sample_count": (10, 100_000, "int"),
    "min_promote_accuracy": (0.80, 1.00, "float"),
    "min_rollback_accuracy": (0.50, 0.99, "float"),
    "initial_canary_pct": (1.0, 50.0, "float"),
    "novelty_threshold": (0.05, 0.95, "float"),
}
BOOL_FIELDS = ("novelty_logging_enabled",)

# JSON (camelCase, like the rest of this service's API) <-> column names
_CAMEL_TO_SNAKE = {
    "minAgreementRate": "min_agreement_rate",
    "minSampleCount": "min_sample_count",
    "minPromoteAccuracy": "min_promote_accuracy",
    "minRollbackAccuracy": "min_rollback_accuracy",
    "initialCanaryPct": "initial_canary_pct",
    "noveltyThreshold": "novelty_threshold",
    "noveltyLoggingEnabled": "novelty_logging_enabled",
}
_SNAKE_TO_CAMEL = {v: k for k, v in _CAMEL_TO_SNAKE.items()}
CONFIG_FIELDS = tuple(_CAMEL_TO_SNAKE.values())


def config_defaults() -> dict:
    """Code defaults, read from the TenantCalibrationConfig dataclass so
    there is a single source of truth (no second copy to drift)."""
    out = {}
    for f in fields(TenantCalibrationConfig):
        if f.name in CONFIG_FIELDS and f.default is not MISSING:
            out[f.name] = f.default
    return out


# ── Request model ────────────────────────────────────────────────────

class CalibrationConfigPatch(BaseModel):
    """Every field optional; see PUT semantics in the module docstring."""

    model_config = ConfigDict(extra="forbid")

    minAgreementRate: float | None = None
    minSampleCount: int | None = None
    minPromoteAccuracy: float | None = None
    minRollbackAccuracy: float | None = None
    initialCanaryPct: float | None = None
    noveltyThreshold: float | None = None
    noveltyLoggingEnabled: bool | None = None


# ── Pure validation (unit-tested without FastAPI/DB) ─────────────────

def merge_and_validate(current: dict, patch: dict) -> tuple[dict, list[str]]:
    """
    current : full snake_case config (stored row or defaults)
    patch   : snake_case subset the caller wants to change (no Nones)
    Returns (merged, errors). errors == [] means merged is safe to save.

    Only PATCHED fields are range-checked, so an out-of-range value
    already in the DB (set by hand before this route existed) does not
    block an unrelated edit. The promote > rollback relation is checked
    whenever either side is patched.
    """
    errors: list[str] = []
    merged = dict(current)

    if not patch:
        return merged, ["no fields to update"]

    for name, value in patch.items():
        if name in BOUNDS:
            lo, hi, kind = BOUNDS[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{_SNAKE_TO_CAMEL[name]} must be a number")
                continue
            if kind == "int":
                if isinstance(value, float) and not value.is_integer():
                    errors.append(f"{_SNAKE_TO_CAMEL[name]} must be an integer")
                    continue
                value = int(value)
            else:
                value = float(value)
            if not (lo <= value <= hi):
                errors.append(f"{_SNAKE_TO_CAMEL[name]} must be between {lo} and {hi}")
                continue
            merged[name] = value
        elif name in BOOL_FIELDS:
            if not isinstance(value, bool):
                errors.append(f"{_SNAKE_TO_CAMEL[name]} must be true or false")
                continue
            merged[name] = value
        else:
            errors.append(f"unknown field: {name}")

    if not errors and ("min_promote_accuracy" in patch or "min_rollback_accuracy" in patch):
        if merged["min_rollback_accuracy"] >= merged["min_promote_accuracy"]:
            errors.append(
                "minRollbackAccuracy must be lower than minPromoteAccuracy "
                f"(got rollback={merged['min_rollback_accuracy']}, "
                f"promote={merged['min_promote_accuracy']})"
            )
    return merged, errors


def to_response(tenant_id: int, cfg: dict, *, is_default: bool, updated_at) -> dict:
    return {
        "tenantId": tenant_id,
        "isDefault": is_default,  # True = no row stored; these are the code defaults
        "config": {_SNAKE_TO_CAMEL[k]: cfg[k] for k in CONFIG_FIELDS},
        "updatedAt": updated_at.isoformat() if hasattr(updated_at, "isoformat") else None,
        "bounds": {
            _SNAKE_TO_CAMEL[k]: {"min": lo, "max": hi} for k, (lo, hi, _) in BOUNDS.items()
        },
    }


# ── DB access (strict: errors propagate, unlike TenantCalibrationConfig.load) ──

async def _tenant_exists(db: AsyncSession, tenant_id: int) -> bool:
    result = await db.execute(
        text("SELECT 1 AS ok FROM tenants WHERE id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    return result.first() is not None


async def _read_stored(db: AsyncSession, tenant_id: int) -> tuple[dict | None, object]:
    """(config dict, updated_at) for a stored row, or (None, None)."""
    result = await db.execute(
        text("""
            SELECT min_agreement_rate, min_sample_count,
                   min_promote_accuracy, min_rollback_accuracy,
                   initial_canary_pct,
                   novelty_threshold, novelty_logging_enabled,
                   updated_at
              FROM tenant_calibration_config
             WHERE tenant_id = :tenant_id
        """),
        {"tenant_id": tenant_id},
    )
    row = result.first()
    if row is None:
        return None, None
    cfg = {
        "min_agreement_rate": float(row.min_agreement_rate),
        "min_sample_count": int(row.min_sample_count),
        "min_promote_accuracy": float(row.min_promote_accuracy),
        "min_rollback_accuracy": float(row.min_rollback_accuracy),
        "initial_canary_pct": float(row.initial_canary_pct),
        "novelty_threshold": float(row.novelty_threshold),
        "novelty_logging_enabled": bool(row.novelty_logging_enabled),
    }
    return cfg, row.updated_at


# ── Routes ───────────────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}/calibration-config")
async def get_calibration_config(tenant_id: int, db: AsyncSession = Depends(get_db)):
    """Current effective config. `isDefault: true` means no row is
    stored and the values shown are the code defaults."""
    if not await _tenant_exists(db, tenant_id):
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id} not found")
    stored, updated_at = await _read_stored(db, tenant_id)
    if stored is None:
        return to_response(tenant_id, config_defaults(), is_default=True, updated_at=None)
    return to_response(tenant_id, stored, is_default=False, updated_at=updated_at)


@router.put("/tenants/{tenant_id}/calibration-config")
async def put_calibration_config(
    tenant_id: int,
    body: CalibrationConfigPatch,
    db: AsyncSession = Depends(get_db),
):
    """Partial-merge update (see module docstring). Returns the config
    as stored after the write."""
    if not await _tenant_exists(db, tenant_id):
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id} not found")

    patch = {
        _CAMEL_TO_SNAKE[k]: v for k, v in body.model_dump().items() if v is not None
    }

    stored, _ = await _read_stored(db, tenant_id)
    current = stored if stored is not None else config_defaults()

    merged, errors = merge_and_validate(current, patch)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    changed = {
        k: (current[k], merged[k]) for k in CONFIG_FIELDS if current[k] != merged[k]
    }

    await TenantCalibrationConfig(tenant_id=tenant_id, **merged).save(db)

    # No audit table exists for config changes; the log line is the trail.
    logger.info(
        "admin calibration-config updated tenant=%s created=%s changed=%s",
        tenant_id, stored is None,
        {k: {"from": a, "to": b} for k, (a, b) in changed.items()},
    )

    stored_after, updated_at = await _read_stored(db, tenant_id)
    return to_response(
        tenant_id, stored_after if stored_after is not None else merged,
        is_default=False, updated_at=updated_at,
    )
