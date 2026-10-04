"""
EXPERIENCE DUPLICATION REPORT -- P6D-8 (read-only, measure-before-build).

docs/EXPERIENCE_DEDUPLICATION_AND_RETENTION.md and
docs/TRAINING_GRADE_DATA_TASK.md both require measuring real
duplication BEFORE committing a reservoir cap (M) or a retention
period (N) -- this script is that measurement. It changes nothing: no
write, no delete, no new table used. It only groups existing
`language_experiences` rows by the shape key from `experience_shape.py`
and reports counts, so the owner's P6D-1/P6D-2 decisions are informed
by real numbers instead of a guess.

    docker compose exec python-api python -m app.language.experience_backfill_report --tenant N
    docker compose exec python-api python -m app.language.experience_backfill_report --tenant N --json

Grouping logic (`summarize_groups`) is pure and unit-tested with fake
rows; only `fetch_rows`/`run_for_tenant` touch a DB, and only with a
plain SELECT. Deliberately reads the open, multi-language tag column
added by migration 017, not the older single-language column that a
separate inventory tool (docs/PENDING_WORK.md A2) tracks for eventual
removal -- so this new module has nothing for that tool to find.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.language.experience_shape import group_key

FETCH_SQL = """
    SELECT experience_id, normalized_message, entity_spans, language,
           brain_prediction->>'intent' AS predicted_intent
      FROM language_experiences
     WHERE tenant_id = :tenant_id
       AND normalized_message IS NOT NULL
     ORDER BY created_at DESC
     LIMIT :limit
"""


@dataclass
class Row:
    experience_id: str
    normalized_message: str | None
    entity_spans: list
    language: str | None
    intent: str | None


@dataclass
class DuplicationSummary:
    total_rows: int
    distinct_groups: int
    largest_groups: list[tuple[str, int]]   # (group key, count), top N
    group_size_histogram: dict[str, int]    # "1", "2-4", "5-19", "20-99", "100+"
    rows_beyond_cap: dict[int, int] = field(default_factory=dict)  # candidate M -> rows saved if capped

    def to_dict(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "distinct_groups": self.distinct_groups,
            "dedup_ratio": round(1 - (self.distinct_groups / self.total_rows), 4) if self.total_rows else None,
            "largest_groups": [{"key": k, "count": n} for k, n in self.largest_groups],
            "group_size_histogram": self.group_size_histogram,
            "rows_saved_at_cap": self.rows_beyond_cap,
        }


def _bucket(n: int) -> str:
    if n == 1: return "1"
    if n <= 4: return "2-4"
    if n <= 19: return "5-19"
    if n <= 99: return "20-99"
    return "100+"


def summarize_groups(rows: list[Row], tenant_id: int, *, top_n: int = 10,
                      candidate_caps: tuple[int, ...] = (10, 25, 50, 100)) -> DuplicationSummary:
    """Pure. Groups rows by (tenant, intent, language, shape) exactly as
    EXPERIENCE_DEDUPLICATION_AND_RETENTION.md §3 defines "same
    experience" -- never by similarity."""
    counts: Counter[str] = Counter()
    for r in rows:
        k = group_key(tenant_id=tenant_id, intent=r.intent, language=r.language,
                       normalized_message=r.normalized_message, entity_spans=r.entity_spans)
        counts[k.as_key()] += 1

    total = sum(counts.values())
    histogram: Counter[str] = Counter()
    for n in counts.values():
        histogram[_bucket(n)] += 1

    saved_at_cap: dict[int, int] = {}
    for cap in candidate_caps:
        saved_at_cap[cap] = sum(max(0, n - cap) for n in counts.values())

    return DuplicationSummary(
        total_rows=total,
        distinct_groups=len(counts),
        largest_groups=counts.most_common(top_n),
        group_size_histogram=dict(sorted(histogram.items())),
        rows_beyond_cap=saved_at_cap,
    )


async def fetch_rows(db, tenant_id: int, limit: int = 200_000) -> list[Row]:  # pragma: no cover -- needs real DB
    from sqlalchemy import text  # lazy
    result = await db.execute(text(FETCH_SQL), {"tenant_id": tenant_id, "limit": limit})
    out = []
    for r in result:
        spans = r.entity_spans if isinstance(r.entity_spans, list) else []
        out.append(Row(str(r.experience_id), r.normalized_message, spans, r.language, r.predicted_intent))
    return out


async def run_for_tenant(db, tenant_id: int) -> DuplicationSummary:  # pragma: no cover -- needs real DB
    rows = await fetch_rows(db, tenant_id)
    return summarize_groups(rows, tenant_id)


async def _main(args) -> int:  # pragma: no cover -- needs real DB
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        summary = await run_for_tenant(db, args.tenant)
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
    else:
        d = summary.to_dict()
        print(f"tenant={args.tenant} rows={d['total_rows']} distinct_groups={d['distinct_groups']} "
              f"dedup_ratio={d['dedup_ratio']}")
        print("size histogram:", d["group_size_histogram"])
        print("rows saved at cap M:", d["rows_saved_at_cap"])
        print("largest groups (key -> count):")
        for g in d["largest_groups"]:
            print(" ", g["key"][:80], "->", g["count"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--json", action="store_true")
    import sys
    sys.exit(asyncio.run(_main(ap.parse_args())))
