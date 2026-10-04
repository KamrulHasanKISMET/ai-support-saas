"""
DRIFT / DEGRADATION DETECTION -- Phase 8 (docs/PHASE_5_8_PLAN.md P8-2).

Two pure detectors over data the system already records:

  1. distribution_shift(baseline_counts, current_counts) -- Population
     Stability Index over categorical counts (language mix, intent mix,
     phenomena mix from turn_understandings).
  2. agreement_drop(base_agree, base_n, cur_agree, cur_n) -- one-sided
     two-proportion z-test: has a brain/model's agreement rate with the
     served intent DROPPED versus its own baseline window?

They report; they never act (no auto rollback here -- that belongs to
the canary services, which already own rollback). Thresholds are
uncalibrated placeholders (THRESHOLDS_CALIBRATED=False): PSI's usual
rule of thumb (0.10 watch / 0.25 act) is a convention, not a fact about
this data. Small samples return status 'insufficient_data' instead of a
verdict, so a quiet tenant never raises a false alarm.

Pure module: math + stdlib.
"""

from __future__ import annotations

import math

THRESHOLDS_CALIBRATED = False
PSI_WATCH = 0.10
PSI_ALERT = 0.25
MIN_SAMPLES = 100          # per window, for either detector
Z_ALERT = 2.33             # ~1% one-sided
MIN_DROP = 0.03            # ignore statistically-real but tiny (<3 pt) drops
EPS = 1e-4                 # smoothing so an empty category cannot give log(0)


def psi(baseline: dict[str, int], current: dict[str, int]) -> float:
    cats = set(baseline) | set(current)
    tb, tc = sum(baseline.values()), sum(current.values())
    if tb <= 0 or tc <= 0:
        raise ValueError("both windows need at least one observation")
    total = 0.0
    for c in cats:
        b = max(baseline.get(c, 0) / tb, EPS)
        a = max(current.get(c, 0) / tc, EPS)
        total += (a - b) * math.log(a / b)
    return total


def distribution_shift(baseline: dict[str, int], current: dict[str, int]) -> dict:
    nb, nc = sum(baseline.values()), sum(current.values())
    if min(nb, nc) < MIN_SAMPLES:
        return {"status": "insufficient_data", "n_baseline": nb, "n_current": nc, "psi": None,
                "thresholds_calibrated": THRESHOLDS_CALIBRATED}
    v = psi(baseline, current)
    status = "alert" if v >= PSI_ALERT else "watch" if v >= PSI_WATCH else "stable"
    tb, tc = nb, nc
    movers = sorted(
        ((c, current.get(c, 0) / tc - baseline.get(c, 0) / tb) for c in set(baseline) | set(current)),
        key=lambda kv: -abs(kv[1]))[:3]
    return {"status": status, "psi": round(v, 4), "n_baseline": nb, "n_current": nc,
            "top_movers": [{"category": c, "share_change": round(d, 4)} for c, d in movers],
            "thresholds_calibrated": THRESHOLDS_CALIBRATED}


def agreement_drop(base_agree: int, base_n: int, cur_agree: int, cur_n: int) -> dict:
    if not (0 <= base_agree <= base_n and 0 <= cur_agree <= cur_n):
        raise ValueError("agree counts must be within 0..n")
    if min(base_n, cur_n) < MIN_SAMPLES:
        return {"status": "insufficient_data", "n_baseline": base_n, "n_current": cur_n,
                "thresholds_calibrated": THRESHOLDS_CALIBRATED}
    p1, p2 = base_agree / base_n, cur_agree / cur_n
    pooled = (base_agree + cur_agree) / (base_n + cur_n)
    se = math.sqrt(pooled * (1 - pooled) * (1 / base_n + 1 / cur_n))
    z = 0.0 if se == 0 else (p1 - p2) / se       # positive = current is worse
    drop = p1 - p2
    status = "degraded" if (z >= Z_ALERT and drop >= MIN_DROP) else "ok"
    return {"status": status, "baseline_rate": round(p1, 4), "current_rate": round(p2, 4),
            "drop": round(drop, 4), "z": round(z, 3), "n_baseline": base_n, "n_current": cur_n,
            "thresholds_calibrated": THRESHOLDS_CALIBRATED}
