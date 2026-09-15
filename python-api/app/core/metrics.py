"""
METRICS FOUNDATION (Phase 1) -- request counters + infra metrics.

Deliberately NOT Prometheus/StatsD format and NOT a new dependency
(no `psutil`, no `prometheus_client`). This is the "safest minimal
way" the task asked for: in-memory counters + stdlib-only process
introspection, exposed as plain JSON from /readiness and a small
/metrics endpoint. Swap this for a real metrics backend in a later
phase without changing where instrumentation calls happen (record_request()
is the one call site every consumer of this module needs).

Process-wide, single-process assumption: uvicorn here runs without
multiple worker processes (see Dockerfile/docker-compose.yml), so a
single in-memory counter is accurate. If workers are ever added later,
these counters become PER-WORKER, not global -- flagged here so a
future change doesn't silently misinterpret them.
"""

import resource
import time
from dataclasses import dataclass, field
from threading import Lock

_process_started_at = time.time()


@dataclass
class _RequestMetrics:
    total_count: int = 0
    error_count: int = 0  # status >= 500
    status_counts: dict[int, int] = field(default_factory=dict)
    total_duration_ms: float = 0.0

    def record(self, status_code: int, duration_ms: float) -> None:
        self.total_count += 1
        if status_code >= 500:
            self.error_count += 1
        self.status_counts[status_code] = self.status_counts.get(status_code, 0) + 1
        self.total_duration_ms += duration_ms


_metrics = _RequestMetrics()
_lock = Lock()


def record_request(status_code: int, duration_ms: float) -> None:
    """Called once per HTTP request by the metrics middleware
    (app/main.py). Thread-safe (uvicorn's async workers still run on
    threads for some operations) but cheap -- a single lock around a
    few integer increments."""
    with _lock:
        _metrics.record(status_code, duration_ms)


def _current_rss_mb() -> float | None:
    """Current resident memory in MB, read from /proc (Linux-only --
    true for every environment this runs in per docker-compose.yml).
    Falls back to peak RSS (resource.ru_maxrss) if /proc is unavailable
    rather than raising -- infra metrics must never break a request."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024, 1)
    except Exception:
        pass
    try:
        # ru_maxrss is KB on Linux, bytes on macOS -- this service only
        # ever runs in the Linux containers defined in docker-compose.yml.
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


def get_infra_snapshot() -> dict:
    """CPU/RAM/uptime -- a foundation, not a monitoring platform.
    `cpu_time_seconds` is CUMULATIVE user+system CPU time consumed
    since process start (not an instantaneous percentage) -- computing
    a true CPU% needs sampling over an interval, which is out of scope
    for this phase. Documented explicitly in docs/OBSERVABILITY.md so
    it isn't misread as "current load"."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "uptime_seconds": round(time.time() - _process_started_at, 1),
        "cpu_time_seconds": round(usage.ru_utime + usage.ru_stime, 2),
        "memory_rss_mb": _current_rss_mb(),
    }


def get_request_metrics_snapshot() -> dict:
    with _lock:
        avg_duration_ms = (
            round(_metrics.total_duration_ms / _metrics.total_count, 2)
            if _metrics.total_count
            else 0.0
        )
        return {
            "total_count": _metrics.total_count,
            "error_count": _metrics.error_count,
            "status_counts": dict(_metrics.status_counts),
            "avg_duration_ms": avg_duration_ms,
        }
