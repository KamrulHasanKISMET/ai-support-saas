"""
METRICS FOUNDATION (Phase 1) -- request counters + infra metrics, PLUS
Production Reliability workstream Prometheus collectors.

Two consumers of the same underlying events, kept in one module rather
than as competing implementations:

  * The original in-memory counters + stdlib process introspection
    (record_request / get_infra_snapshot / get_request_metrics_snapshot)
    -- still the data source for GET /health and GET /readiness's
    `process` field. NOT Prometheus format; deliberately simple.

  * Prometheus collectors (http_request_duration_seconds,
    http_requests_total, http_errors_total, db_query_duration_seconds,
    db_pool_*) -- scraped at GET /metrics (api/routes/metrics.py),
    mirroring node-api's `middleware/metrics.ts` +
    `observability/registry.ts`. See docs/RELIABILITY.md.

This module is a leaf: it must NOT import `app.core.database`.
`database.py` imports `db_query_duration_seconds` from here to time
queries; if this module imported the engine back, that would be a
circular import. Pool-state gauges are instead populated on-demand by
whichever route reads them (see api/routes/metrics.py), which is free
to import both this module and the engine.

Process-wide, single-process assumption: uvicorn here runs without
multiple worker processes (see Dockerfile/docker-compose.yml), so a
single in-memory counter/registry is accurate. If workers are ever
added later, these become PER-WORKER, not global -- flagged here so a
future change doesn't silently misinterpret them. (Prometheus's own
multiprocess mode would be needed at that point -- see
docs/RELIABILITY.md "Scaling triggers".)
"""
try:
    import resource
except ImportError:
    resource = None

import time
from dataclasses import dataclass, field
from threading import Lock

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

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
    (app/core/request_middleware.py). Thread-safe (uvicorn's async
    workers still run on threads for some operations) but cheap -- a
    single lock around a few integer increments."""
    with _lock:
        _metrics.record(status_code, duration_ms)



def _current_rss_mb() -> float | None:
    """Return current resident memory in MB.

    Linux: read VmRSS from /proc.
    Windows: use the native GetProcessMemoryInfo API.
    Other environments: fall back to resource.ru_maxrss when available.
    """
    # Linux
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024, 1)
    except Exception:
        pass

    # Windows
    if resource is None:
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)

            process = ctypes.windll.kernel32.GetCurrentProcess()
            get_memory = ctypes.windll.psapi.GetProcessMemoryInfo

            get_memory.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            get_memory.restype = wintypes.BOOL

            if get_memory(
                process,
                ctypes.byref(counters),
                counters.cb,
            ):
                return round(
                    counters.WorkingSetSize / (1024 * 1024),
                    1,
                )
        except Exception:
            pass

        return None

    # Linux/macOS resource fallback
    try:
        return round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            1,
        )
    except Exception:
        return None


def get_infra_snapshot() -> dict:
    """CPU/RAM/uptime -- a foundation, not a monitoring platform.
    `cpu_time_seconds` is CUMULATIVE user+system CPU time consumed
    since process start (not an instantaneous percentage) -- computing
    a true CPU% needs sampling over an interval, which is out of scope
    for this phase. Documented explicitly in docs/OBSERVABILITY.md so
    it isn't misread as "current load"."""

    if resource is not None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_time_seconds = usage.ru_utime + usage.ru_stime
    else:
        cpu_time_seconds = time.process_time()
    return {
        "uptime_seconds": round(time.time() - _process_started_at, 1),
        "cpu_time_seconds": round(cpu_time_seconds, 2),
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


# ---------------------------------------------------------------------
# Production Reliability workstream -- Prometheus collectors.
# ---------------------------------------------------------------------

# A single process (one uvicorn worker per container, per the
# Dockerfile CMD) — a plain registry is correct. If that ever changes
# (e.g. `uvicorn --workers > 1` in one container), this needs
# prometheus_client's multiprocess mode (PROMETHEUS_MULTIPROC_DIR +
# MultiProcessCollector) instead — see docs/RELIABILITY.md "Scaling
# triggers" before adding workers.
registry = CollectorRegistry()

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds, labeled by method/route/status_code",
    labelnames=["method", "route", "status_code"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=registry,
)

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    labelnames=["method", "route", "status_code"],
    registry=registry,
)

http_errors_total = Counter(
    "http_errors_total",
    "Total HTTP responses with status_code >= 400",
    labelnames=["method", "route", "status_code"],
    registry=registry,
)

# Mirrors node-api's db_query_duration_seconds (config/database.ts) —
# same metric name/intent, populated by SQLAlchemy cursor events in
# app/core/database.py.
db_query_duration_seconds = Histogram(
    "db_query_duration_seconds",
    "PostgreSQL query duration in seconds, as observed by python-api's SQLAlchemy engine",
    labelnames=["status"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=registry,
)

# Pool saturation — the leading indicator for "pool exhaustion" alerts
# (see monitoring/alerts.yml), set at scrape time from the SQLAlchemy
# engine's pool (see api/routes/metrics.py).
db_pool_size = Gauge(
    "db_pool_size",
    "Configured SQLAlchemy pool size (excludes overflow) for python-api",
    registry=registry,
)
db_pool_checked_out = Gauge(
    "db_pool_checked_out_connections",
    "Connections currently checked out of the SQLAlchemy pool",
    registry=registry,
)
db_pool_overflow = Gauge(
    "db_pool_overflow_connections",
    "Overflow connections currently open beyond the base pool size (>0 sustained = approaching max_overflow)",
    registry=registry,
)
