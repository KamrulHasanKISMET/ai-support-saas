// Production Reliability workstream -- reproducible load test.
// Run via `./loadtest/run.sh`, or directly: `k6 run loadtest/script.js`.
// See docs/RELIABILITY.md "Load testing" and loadtest/README.md.
//
// Two scenarios, chosen with `-e SCENARIO=health|messages` (default
// "health"):
//
//   health   -- GET /health on node-api and python-api. No auth, no
//               DB writes, no LLM calls. Measures raw HTTP-layer
//               capacity (event loop, connection handling, the infra
//               this workstream is actually responsible for) without
//               conflating it with LLM latency/cost.
//
//   messages -- the real, authenticated, end-to-end path: POST
//               /messages on node-api, which resolves a customer's
//               conversation, calls python-api's Kernel, and (if
//               ANTHROPIC_API_KEY is configured) makes a real LLM
//               call. This is the realistic number, but it costs real
//               API credits and is rate-limited by the LLM provider,
//               not just by this stack -- see loadtest/README.md
//               before running it past a handful of VUs.
//
// Either way: this script does NOT invent capacity numbers. It only
// runs the requests and reports what k6 measured -- see
// docs/RELIABILITY.md for the honest status of whether this has been
// run against a live deployment yet.

import http from "k6/http";
import { check, sleep } from "k6";
import { Rate } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:4000";
const SCENARIO = __ENV.SCENARIO || "health";
const API_KEY = __ENV.TEST_API_KEY || "";

const errorRate = new Rate("errors");

// Staged, increasing concurrency -- the "reproducible system for
// testing increasing concurrency" requested for this workstream.
// Deliberately conservative defaults for the `messages` scenario given
// it drives real LLM calls; override with `-e VUS_MAX=...` /
// `--stage` flags, or edit directly, once you know your provider's
// rate limits.
const STAGES =
  SCENARIO === "messages"
    ? [
        { duration: "30s", target: 2 },
        { duration: "1m", target: 5 },
        { duration: "1m", target: 10 },
        { duration: "30s", target: 0 },
      ]
    : [
        { duration: "30s", target: 10 },
        { duration: "1m", target: 50 },
        { duration: "1m", target: 100 },
        { duration: "1m", target: 200 },
        { duration: "30s", target: 0 },
      ];

export const options = {
  stages: STAGES,
  thresholds: {
    // Pass/fail gates for CI/manual runs -- NOT a claimed capacity
    // number. A run that violates these tells you where the ceiling
    // is; it doesn't by itself say "the system handles N req/s".
    http_req_duration: ["p(95)<1000", "p(99)<3000"],
    errors: ["rate<0.01"],
  },
};

export function setup() {
  if (SCENARIO === "messages" && !API_KEY) {
    throw new Error(
      "SCENARIO=messages requires TEST_API_KEY (a tenant api_key from POST /auth/signup). " +
        "See loadtest/README.md for how to create one."
    );
  }
}

export default function () {
  if (SCENARIO === "messages") {
    runMessagesScenario();
  } else {
    runHealthScenario();
  }
  sleep(1);
}

function runHealthScenario() {
  const nodeRes = http.get(`${BASE_URL}/health`);
  check(nodeRes, { "node-api /health is 200": (r) => r.status === 200 }) ||
    errorRate.add(1);

  const pythonBase = __ENV.PYTHON_BASE_URL || "http://localhost:8000";
  const pythonRes = http.get(`${pythonBase}/health`);
  check(pythonRes, { "python-api /health is 200": (r) => r.status === 200 }) ||
    errorRate.add(1);
}

function runMessagesScenario() {
  const headers = { "Content-Type": "application/json", "x-api-key": API_KEY };

  const customerRes = http.post(
    `${BASE_URL}/customers`,
    JSON.stringify({ displayName: `loadtest-${__VU}-${__ITER}` }),
    { headers }
  );
  const customerOk = check(customerRes, {
    "create customer is 201": (r) => r.status === 201,
  });
  if (!customerOk) {
    errorRate.add(1);
    return;
  }
  const customerId = customerRes.json("id");

  const messageRes = http.post(
    `${BASE_URL}/messages`,
    JSON.stringify({
      customerId,
      channel: "website",
      content: "Hi, what are your store hours?",
    }),
    { headers, timeout: "30s" }
  );
  check(messageRes, { "send message is 2xx": (r) => r.status >= 200 && r.status < 300 }) ||
    errorRate.add(1);
}
