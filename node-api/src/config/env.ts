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
};
