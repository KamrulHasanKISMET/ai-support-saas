"""
PRICING & SAVINGS -- Phase 8 (docs/PHASE_5_8_PLAN.md P8-1).
GENERAL_LANGUAGE_BRAIN.md §8: report cost savings in currency, not only tokens.

DELIBERATELY EMPTY: no price is hard-coded here. Prices change and a
made-up number would turn into a false savings claim on a dashboard.
An operator supplies the table (MODEL_PRICING_JSON env or a dict):

    {"claude-sonnet-5": {"input_per_mtok": <number>, "output_per_mtok": <number>,
                          "currency": "USD"}}

Until a price exists for a model, every currency figure is None
("unknown"), never 0 -- the two are never conflated.

What "saved" means (honest definition): a turn served by the own brain/
model skipped the LLM Intent Engine call. Savings = calls_saved x the
MEASURED mean tokens of an Intent Engine call x price. The mean must be
measured (from turns that did call it); if it was not measured,
savings is None. It is an estimate, and `estimate=True` says so.

Pure module: stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

TOKENS_PER_MTOK = 1_000_000


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    currency: str = "USD"


def parse_pricing(raw: str | dict | None) -> dict[str, ModelPrice]:
    """Strict: a malformed table is an error, not silently empty."""
    if raw in (None, "", {}):
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        out: dict[str, ModelPrice] = {}
        for model, p in data.items():
            i, o = float(p["input_per_mtok"]), float(p["output_per_mtok"])
            if i < 0 or o < 0 or i != i or o != o:
                raise PricingError(f"negative/NaN price for {model}")
            out[str(model)] = ModelPrice(i, o, str(p.get("currency", "USD")))
        return out
    except PricingError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise PricingError(f"malformed pricing table: {exc}") from exc


def cost(model: str, input_tokens: int | None, output_tokens: int | None,
         table: dict[str, ModelPrice]) -> float | None:
    """None when the model has no price OR usage is unknown (NULL != 0)."""
    p = table.get(model)
    if p is None or input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * p.input_per_mtok + output_tokens * p.output_per_mtok) / TOKENS_PER_MTOK


def estimate_savings(
    *, model: str, calls_saved: int, mean_input_tokens: float | None,
    mean_output_tokens: float | None, table: dict[str, ModelPrice],
) -> dict:
    """Estimated currency saved by skipped Intent Engine calls."""
    base = {"estimate": True, "calls_saved": calls_saved, "currency": None, "saved": None,
            "why_unknown": None}
    if calls_saved < 0:
        raise PricingError("calls_saved cannot be negative")
    if mean_input_tokens is None or mean_output_tokens is None:
        base["why_unknown"] = "intent-call token mean not measured"
        return base
    p = table.get(model)
    if p is None:
        base["why_unknown"] = f"no price configured for {model}"
        return base
    per_call = cost(model, mean_input_tokens, mean_output_tokens, table)
    base.update(currency=p.currency, saved=calls_saved * per_call)
    return base
