import "dotenv/config";

function required(name: string, fallback?: string): string {
  const value = process.env[name] ?? fallback;
  if (value === undefined) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

export const env = {
  nodeEnv: process.env.NODE_ENV ?? "development",
  port: Number(process.env.PORT ?? 4000),

  databaseUrl: required("DATABASE_URL"),
  redisUrl: required("REDIS_URL"),
  pythonApiUrl: required("PYTHON_API_URL"),

  // Production Reliability workstream: explicit, tunable pool/timeout
  // settings instead of pg's silent defaults. See docs/RELIABILITY.md
  // "Scaling triggers" for how DB_POOL_MAX should relate to Postgres's
  // own max_connections across all node-api replicas + python-api.
  dbPoolMax: Number(process.env.DB_POOL_MAX ?? 10),
  dbPoolIdleTimeoutMs: Number(process.env.DB_POOL_IDLE_TIMEOUT_MS ?? 30_000),
  dbPoolConnectionTimeoutMs: Number(process.env.DB_POOL_CONNECTION_TIMEOUT_MS ?? 5_000),
  dbStatementTimeoutMs: Number(process.env.DB_STATEMENT_TIMEOUT_MS ?? 15_000),

  jwtSecret: required("JWT_SECRET", "change-me-in-production"),
  internalServiceSecret: required(
    "INTERNAL_SERVICE_SECRET",
    "change-me-to-a-long-random-string"
  ),

  // WhatsApp Cloud API -- ONE Meta App serves every tenant's phone
  // number, so these are global, not per-tenant. Per-tenant data
  // (which phone number, which access token) lives in
  // channel_credentials -- see channel_credentials.repository.ts.
  whatsappVerifyToken: process.env.WHATSAPP_VERIFY_TOKEN ?? "",
  whatsappAppSecret: process.env.WHATSAPP_APP_SECRET ?? "",
  whatsappGraphApiBaseUrl:
    process.env.WHATSAPP_GRAPH_API_BASE_URL ?? "https://graph.facebook.com/v21.0",

  // Knowledge Base document storage (CUSTOMER-KNOWLEDGE-RAG-API-001).
  // Local disk by default -- see utils/storage.ts's StorageProvider
  // abstraction for how this would swap to S3/object storage later
  // without touching call sites. Mounted as a named Docker volume
  // (docker-compose.yml: node_uploads) so files survive a container
  // restart.
  uploadsDir: process.env.UPLOADS_DIR ?? "/app/uploads",
  maxUploadSizeBytes: Number(process.env.MAX_UPLOAD_SIZE_BYTES ?? 20 * 1024 * 1024), // 20MB
};
