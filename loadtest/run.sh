#!/usr/bin/env bash
# Runs the k6 load test and, in parallel, samples `docker stats`
# (CPU/RAM per container) and each service's /metrics endpoint (DB
# pool state), so a single run produces throughput + latency + errors
# (from k6) AND CPU/RAM/DB-connections (from these samples) together --
# the full set this workstream's load-testing requirement asks for.
#
# Requires: k6 (https://k6.io/docs/get-started/installation/) and a
# running stack (`docker compose up`, or `up -d`).
#
# Usage:
#   ./loadtest/run.sh                          # SCENARIO=health, defaults
#   SCENARIO=messages TEST_API_KEY=xxx ./loadtest/run.sh
set -euo pipefail

SCENARIO="${SCENARIO:-health}"
BASE_URL="${BASE_URL:-http://localhost:4000}"
PYTHON_BASE_URL="${PYTHON_BASE_URL:-http://localhost:8000}"
RESULTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/results/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${RESULTS_DIR}"

if ! command -v k6 > /dev/null 2>&1; then
  echo "k6 is not installed. Install it (see https://k6.io/docs/get-started/installation/)" >&2
  echo "or run via Docker: docker run --rm -i --network host grafana/k6 run - < loadtest/script.js" >&2
  exit 1
fi

sample_resources() {
  # Runs until killed (see trap below). One line of `docker stats`
  # output per container per interval, plus a raw /metrics scrape from
  # each service -- enough to correlate "latency went up" (k6) with
  # "CPU/RAM/DB-pool went up" (this) at the same point in the run.
  while true; do
    {
      echo "=== $(date -Iseconds) ==="
      docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" 2>/dev/null || true
    } >> "${RESULTS_DIR}/docker_stats.log"

    curl -s "${BASE_URL}/metrics" 2>/dev/null | grep -E "^db_pool_|^http_requests_total|^http_errors_total" \
      >> "${RESULTS_DIR}/node_api_metrics_samples.log" || true
    echo "--- $(date -Iseconds) ---" >> "${RESULTS_DIR}/node_api_metrics_samples.log"

    curl -s "${PYTHON_BASE_URL}/metrics" 2>/dev/null | grep -E "^db_pool_|^http_requests_total|^http_errors_total" \
      >> "${RESULTS_DIR}/python_api_metrics_samples.log" || true
    echo "--- $(date -Iseconds) ---" >> "${RESULTS_DIR}/python_api_metrics_samples.log"

    sleep 5
  done
}

sample_resources &
SAMPLER_PID=$!
trap 'kill "${SAMPLER_PID}" 2>/dev/null || true' EXIT

echo "[run.sh] scenario=${SCENARIO} base_url=${BASE_URL} -- results in ${RESULTS_DIR}"

k6 run \
  --env SCENARIO="${SCENARIO}" \
  --env BASE_URL="${BASE_URL}" \
  --env PYTHON_BASE_URL="${PYTHON_BASE_URL}" \
  --env TEST_API_KEY="${TEST_API_KEY:-}" \
  --summary-export "${RESULTS_DIR}/k6_summary.json" \
  "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/script.js" \
  | tee "${RESULTS_DIR}/k6_output.txt"

echo "[run.sh] done. Results: ${RESULTS_DIR}"
echo "[run.sh] fill the load-test results table in docs/RELIABILITY.md from k6_summary.json + docker_stats.log"
