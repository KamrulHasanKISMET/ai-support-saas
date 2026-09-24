import { strict as assert } from "node:assert";
import { test } from "node:test";
import http from "node:http";
import express from "express";
import { metricsMiddleware, metricsHandler, registry } from "./metrics";

function listen(app: express.Express): Promise<{ port: number; close: () => Promise<void> }> {
  return new Promise((resolve) => {
    const server = app.listen(0, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      resolve({
        port,
        close: () => new Promise((res) => server.close(() => res())),
      });
    });
  });
}

function get(port: number, path: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    http
      .get(`http://127.0.0.1:${port}${path}`, (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, body }));
      })
      .on("error", reject);
  });
}

test("metricsMiddleware records request count/duration by matched route, and /metrics exposes them", async () => {
  registry.resetMetrics();

  const app = express();
  app.use(metricsMiddleware);
  app.get("/widgets/:id", (req, res) => res.json({ id: req.params.id }));
  app.get("/boom", (_req, res) => res.status(500).json({ error: "BOOM" }));
  app.get("/metrics", metricsHandler);

  const { port, close } = await listen(app);
  try {
    await get(port, "/widgets/123");
    await get(port, "/widgets/456");
    await get(port, "/boom");

    const { status, body } = await get(port, "/metrics");
    assert.equal(status, 200);

    // Route label should be the matched pattern ("/widgets/:id"), not the
    // raw path -- this is what keeps metric cardinality bounded.
    assert.match(
      body,
      /http_requests_total\{method="GET",route="\/widgets\/:id",status_code="200"\} 2/
    );
    assert.match(
      body,
      /http_errors_total\{method="GET",route="\/boom",status_code="500"\} 1/
    );
    assert.match(body, /http_request_duration_seconds_count\{method="GET",route="\/widgets\/:id",status_code="200"\} 2/);
    assert.match(body, /db_pool_total_connections \d/);
  } finally {
    await close();
  }
});
