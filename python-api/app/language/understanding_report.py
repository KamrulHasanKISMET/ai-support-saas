"""
UNDERSTANDING REPORT -- first reader of turn_understandings
(docs/PENDING_WORK.md C1: "nothing reads the table yet").

Read-only, per-tenant summary of what the Brain has actually been
seeing: language / script mix, share of code-mixed, transliterated,
ambiguous, novel and low-confidence turns, how much intent work the own
Brain (vs the LLM) did, and how many turns are not automatable.

Why it matters: this is the evidence the later phases are waiting for --
which language variants really occur (so eval/native-review effort goes
where traffic is), whether the open language tag is populated (a second
view on the `detectedLanguage` removal question), and how big the
novel/code-mixed slices are before anyone builds a mechanism for them.

It reports counts and shares. It applies NO judgement thresholds beyond
"sample too small" and "language unresolved" (both factual); it does
not say a rate is good or bad -- that needs real data to define.

Tenant isolation: every query is scoped by tenant_id. No message text
exists in the source table (see the migration), so none can leak here.

    python -m app.language.understanding_report --tenant 1 --days 30
"""

from __future__ import annotations

import argparse
import asyncio
import json

MIN_TURNS_FOR_RATES = 30      # below this, rates are shown but flagged as noise
TOP_N = 10

_PHENOMENA = ("code_mixed", "transliterated", "ambiguous", "novel", "low_confidence")


def _rate(n: int, total: int) -> float | None:
    return round(n / total, 4) if total else None


def build_report(counts: dict, by_language: list[tuple[str, int]], by_script: list[tuple[str, int]],
                 *, tenant_id: int | None, days: int) -> dict:
    """Pure. `counts` keys: total, und_language, brain, llm,
    automation_ineligible, code_mixed, transliterated, ambiguous, novel,
    low_confidence. Missing keys count as 0."""
    c = {k: int(counts.get(k) or 0) for k in
         ("total", "und_language", "brain", "llm", "automation_ineligible", *_PHENOMENA)}
    total = c["total"]
    resolved = c["brain"] + c["llm"]
    warnings: list[str] = []
    if total == 0:
        warnings.append("no turns in window")
    elif total < MIN_TURNS_FOR_RATES:
        warnings.append(f"only {total} turns (< {MIN_TURNS_FOR_RATES}); rates are noise")
    if c["und_language"]:
        warnings.append(f"{c['und_language']} turns have language='und' ({_rate(c['und_language'], total):.2%}); "
                        "the open language tag was not resolved for them")

    def share(rows: list[tuple[str, int]]) -> list[dict]:
        return [{"value": v, "turns": n, "share": _rate(n, total)} for v, n in rows[:TOP_N]]

    return {
        "tenant_id": tenant_id,
        "window_days": days,
        "total_turns": total,
        "phenomena": {p: {"turns": c[p], "share": _rate(c[p], total)} for p in _PHENOMENA},
        "language_unresolved": {"turns": c["und_language"], "share": _rate(c["und_language"], total)},
        "intent_source": {
            "brain": c["brain"], "llm": c["llm"],
            "unattributed": max(0, total - resolved),
            "brain_share_of_attributed": _rate(c["brain"], resolved),
        },
        "automation_ineligible": {"turns": c["automation_ineligible"], "share": _rate(c["automation_ineligible"], total)},
        "by_language": share(by_language),
        "by_script": share(by_script),
        "warnings": warnings,
    }


async def fetch(db, tenant_id: int, days: int = 30) -> dict:
    """Aggregate in SQL (never pulls rows). Raises on DB error."""
    from sqlalchemy import text

    days = max(1, min(int(days), 90))   # table only retains 90 days
    params = {"t": tenant_id, "d": days}
    where = "tenant_id = :t AND created_at >= CURRENT_TIMESTAMP - make_interval(days => :d)"
    row = (await db.execute(text(f"""
        SELECT COUNT(*)                                              AS total,
               COUNT(*) FILTER (WHERE language = 'und')              AS und_language,
               COUNT(*) FILTER (WHERE intent_source = 'brain')       AS brain,
               COUNT(*) FILTER (WHERE intent_source = 'llm')         AS llm,
               COUNT(*) FILTER (WHERE NOT automation_eligible)       AS automation_ineligible,
               COUNT(*) FILTER (WHERE 'code_mixed'     = ANY(phenomena)) AS code_mixed,
               COUNT(*) FILTER (WHERE 'transliterated' = ANY(phenomena)) AS transliterated,
               COUNT(*) FILTER (WHERE 'ambiguous'      = ANY(phenomena)) AS ambiguous,
               COUNT(*) FILTER (WHERE 'novel'          = ANY(phenomena)) AS novel,
               COUNT(*) FILTER (WHERE 'low_confidence' = ANY(phenomena)) AS low_confidence
          FROM turn_understandings WHERE {where}
    """), params)).first()
    counts = {k: getattr(row, k) for k in
              ("total", "und_language", "brain", "llm", "automation_ineligible", *_PHENOMENA)}

    async def top(col: str) -> list[tuple[str, int]]:
        res = await db.execute(text(
            f"SELECT {col} AS v, COUNT(*) AS n FROM turn_understandings WHERE {where} "
            f"GROUP BY {col} ORDER BY n DESC, v LIMIT {TOP_N}"), params)
        return [(r.v, int(r.n)) for r in res.all()]

    return build_report(counts, await top("language"), await top("script"), tenant_id=tenant_id, days=days)


async def _main(tenant_id: int, days: int) -> int:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        print(json.dumps(await fetch(db, tenant_id, days), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Per-tenant summary of turn_understandings.")
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_main(args.tenant, args.days)))
