const toInt = (value: string | undefined, fallback: number): number => {
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
};

export const env = {
  NODE_ENV: process.env.NODE_ENV ?? "development",
  PORT: toInt(process.env.PORT, 3001),
  LOG_LEVEL: process.env.LOG_LEVEL ?? "info",
  CACHE_TTL_SECONDS: toInt(process.env.CACHE_TTL_SECONDS, 300),
  REQUEST_TIMEOUT_MS: toInt(process.env.REQUEST_TIMEOUT_MS, 15000),
  EXTRACTION_RETRIES: toInt(process.env.EXTRACTION_RETRIES, 3),
  EXTRACTION_CONCURRENCY: toInt(process.env.EXTRACTION_CONCURRENCY, 4)
};
