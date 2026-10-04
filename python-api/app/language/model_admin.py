"""
MODEL ADMIN CLI -- Phase 6 P6-2a (docs/PHASE_5_8_PLAN.md).

Thin, human-operated wrapper over model_registry.py, model_shadow.py and
model_canary_service.start_canary(). No new logic: every safety rule
(legal transitions, shadow requires a passing offline eval, a reason is
mandatory) lives in model_registry.py and is exercised here, not
reimplemented.

    # see what exists
    python -m app.language.model_admin list --tenant 3

    # read shadow evidence for a model
    python -m app.language.model_admin shadow-report --tenant 3 --version intent-lr-20261001120000 --days 14

    # move it along the lifecycle (each requires --reason)
    python -m app.language.model_admin promote --tenant 3 --version intent-lr-... --to shadow --reason "offline eval passed, see report"
    python -m app.language.model_admin start-canary --tenant 3 --version intent-lr-... --pct 5 --reason "shadow: agreement 0.97 over 480 turns"
    python -m app.language.model_admin retire --tenant 3 --version intent-lr-... --reason "superseded"

`promote` calls model_registry.set_status directly (for shadow/active/
retired/rejected moves); `start-canary` is its own subcommand because it
also creates the model_canary_state row.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.language import model_canary_service as mcs
from app.language import model_registry as mr
from app.language import model_shadow as ms


async def _list(db, tenant_id: int, capability: str) -> list[dict]:
    from sqlalchemy import text
    rows = await db.execute(text("""
        SELECT version, status, created_at, status_changed_at, status_reason,
               (eval_report->>'ok')::boolean AS eval_ok, (eval_report->>'accuracy')::float AS accuracy
          FROM language_models
         WHERE tenant_id = :t AND capability = :c
         ORDER BY created_at DESC
    """), {"t": tenant_id, "c": capability})
    return [dict(version=r.version, status=r.status, created_at=str(r.created_at),
                 status_reason=r.status_reason, eval_ok=r.eval_ok, accuracy=r.accuracy) for r in rows]


async def _dispatch(args) -> int:  # pragma: no cover -- needs real DB
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        if args.cmd == "list":
            print(json.dumps(await _list(db, args.tenant, args.capability), indent=2, default=str))
            return 0
        if args.cmd == "shadow-report":
            report = await ms.report_for(db, args.tenant, args.version, days=args.days)
            print(json.dumps(report, indent=2, default=str))
            return 0
        if args.cmd == "promote":
            await mr.set_status(db, tenant_id=args.tenant, version=args.version, target=args.to,
                                 reason=args.reason, capability=args.capability)
            print(f"{args.version}: -> {args.to}")
            return 0
        if args.cmd == "start-canary":
            await mcs.start_canary(db, tenant_id=args.tenant, version=args.version,
                                    capability=args.capability, initial_pct=args.pct, reason=args.reason)
            print(f"{args.version}: canary started at {args.pct}%")
            return 0
        if args.cmd == "retire":
            await mr.set_status(db, tenant_id=args.tenant, version=args.version, target="retired",
                                 reason=args.reason, capability=args.capability)
            print(f"{args.version}: -> retired")
            return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--tenant", type=int, required=True)
        p.add_argument("--capability", default="intent")

    p = sub.add_parser("list"); common(p)
    p = sub.add_parser("shadow-report"); common(p); p.add_argument("--version", required=True); p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("promote"); common(p); p.add_argument("--version", required=True)
    p.add_argument("--to", required=True, choices=sorted(mr.STATUSES)); p.add_argument("--reason", required=True)
    p = sub.add_parser("start-canary"); common(p); p.add_argument("--version", required=True)
    p.add_argument("--pct", type=float, default=5.0); p.add_argument("--reason", required=True)
    p = sub.add_parser("retire"); common(p); p.add_argument("--version", required=True); p.add_argument("--reason", required=True)
    return ap


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(_dispatch(build_parser().parse_args())))
