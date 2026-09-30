"""
LANGUAGE TAG READINESS -- the measurable trigger for removing the legacy
`detectedLanguage` field (docs/LANGUAGE_INTELLIGENCE.md, Phase 5 Track 1;
docs/GENERAL_LANGUAGE_BRAIN.md §5.2).

The removal rule is "every language_experiences row carries a real
`language` (!= 'und') on real traffic". Rows written before migration
017 are 'und' forever and can never satisfy that, so the check is made
over a WINDOW of rows (--since) that started after the open tag went
live. This module only MEASURES and reports READY / NOT READY -- it
never removes or alters anything. Removal itself is a Phase 6 code
change made by a human once this reports READY on real traffic.

Run (needs DB):
    docker compose exec python-api python -m app.language.language_tag_readiness --since 2026-10-01
    docker compose exec python-api python -m app.language.language_tag_readiness --since 2026-10-01 --tenant 1

Exit code: 0 = READY, 1 = NOT READY (usable in CI / a scheduled check).

HARD BOUNDARY (§5.3): language is counted only as a data-quality
signal, never used to infer anything about a person.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

# Placeholders until real traffic exists -- same honesty as the eval gate.
MIN_ROWS_FOR_DECISION = 500       # a tiny window proves nothing
MAX_UND_RATE = 0.0                # the documented rule: no 'und' rows at all


@dataclass(frozen=True)
class ReadinessResult:
    total_rows: int
    und_rows: int
    und_rate: float
    ready: bool
    reasons: list[str]


def assess(
    total_rows: int,
    und_rows: int,
    *,
    min_rows: int = MIN_ROWS_FOR_DECISION,
    max_und_rate: float = MAX_UND_RATE,
) -> ReadinessResult:
    """Pure decision. Insufficient traffic is NOT READY (never a pass)."""
    total = max(0, int(total_rows))
    und = min(max(0, int(und_rows)), total)
    rate = (und / total) if total else 0.0
    reasons: list[str] = []
    if total < min_rows:
        reasons.append(f"only {total} rows in window (< {min_rows}) -- not enough real traffic to decide")
    if total and rate > max_und_rate:
        reasons.append(f"{und} of {total} rows ({rate:.2%}) still have language='und' (max allowed {max_und_rate:.2%})")
    return ReadinessResult(total, und, round(rate, 6), not reasons, reasons)


async def measure(db, since: str, tenant_id: int | None = None) -> tuple[int, int]:
    """(total_rows, und_rows) for rows created on/after `since`."""
    from datetime import datetime

    from sqlalchemy import text

    since_dt = datetime.fromisoformat(since)
    where = "created_at >= :since"
    params: dict = {"since": since_dt}
    if tenant_id is not None:
        where += " AND tenant_id = :t"
        params["t"] = tenant_id
    row = (
        await db.execute(
            text(
                "SELECT COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE language = 'und') AS und "
                f"FROM language_experiences WHERE {where}"
            ),
            params,
        )
    ).first()
    return int(row.total or 0), int(row.und or 0)


async def _main(since: str, tenant_id: int | None) -> int:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        total, und = await measure(db, since, tenant_id)
    result = assess(total, und)
    print(json.dumps({**asdict(result), "since": since, "tenant": tenant_id}, indent=2))
    print("\nDETECTEDLANGUAGE REMOVAL:", "READY" if result.ready else "NOT READY")
    for r in result.reasons:
        print(" -", r)
    return 0 if result.ready else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Is the open `language` tag populated enough to remove detectedLanguage?")
    ap.add_argument("--since", required=True, help="ISO date/time after which rows are real post-migration-017 traffic")
    ap.add_argument("--tenant", type=int, default=None)
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_main(args.since, args.tenant)))
