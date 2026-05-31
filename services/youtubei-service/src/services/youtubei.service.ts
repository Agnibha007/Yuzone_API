import pLimit from "p-limit";
import { Innertube } from "youtubei.js";
import { env } from "../config/env.js";
import { TTLCache } from "../utils/cache.js";
import { ServiceError } from "../utils/errors.js";
import type { ExtractMetrics, ExtractResponse } from "../types/extract.js";

const cache = new TTLCache<ExtractResponse>(env.CACHE_TTL_SECONDS * 1000);
const limiter = pLimit(Math.max(1, env.EXTRACTION_CONCURRENCY));

let innertubeClientPromise: Promise<Innertube> | null = null;

const metrics: ExtractMetrics = {
  requestsTotal: 0,
  successTotal: 0,
  failureTotal: 0,
  retryTotal: 0,
  cacheHitTotal: 0,
  lastError: null
};

const sleep = async (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const withTimeout = async <T>(promise: Promise<T>, timeoutMs: number): Promise<T> => {
  let timeoutHandle: NodeJS.Timeout | null = null;
  const timeoutPromise = new Promise<T>((_, reject) => {
    timeoutHandle = setTimeout(() => reject(new ServiceError(504, "youtubei extraction timed out")), timeoutMs);
  });

  try {
    return await Promise.race([promise, timeoutPromise]);
  } finally {
    if (timeoutHandle) clearTimeout(timeoutHandle);
  }
};

const getClient = async (): Promise<Innertube> => {
  if (!innertubeClientPromise) {
    innertubeClientPromise = Innertube.create({
      generate_session_locally: true,
      lang: "en",
      location: "US"
    });
  }

  return innertubeClientPromise;
};

const pickBestThumbnail = (value: unknown): string | null => {
  if (!Array.isArray(value) || value.length === 0) return null;
  const urls = value
    .map((item) => (item && typeof item === "object" ? (item as { url?: string }).url : undefined))
    .filter((url): url is string => typeof url === "string");
  return urls.length > 0 ? urls[urls.length - 1] : null;
};

const parseDuration = (basicInfo: Record<string, unknown>): number | null => {
  const candidates = [basicInfo.duration_seconds, basicInfo.duration, basicInfo.length_seconds];
  for (const candidate of candidates) {
    const parsed = Number(candidate);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return null;
};

const resolveAudioUrl = async (format: any, client: Innertube): Promise<string | null> => {
  if (!format) return null;

  if (typeof format.url === "string" && format.url.startsWith("http")) {
    return format.url;
  }

  if (typeof format.decipher === "function") {
    const deciphered = await format.decipher(client.session.player);
    if (typeof deciphered === "string" && deciphered.startsWith("http")) {
      return deciphered;
    }
    if (deciphered && typeof deciphered === "object" && typeof deciphered.url === "string") {
      return deciphered.url;
    }
  }

  return null;
};

const getSortedAudioFormats = (info: any): any[] => {
  const streamingData = info?.streaming_data ?? info?.streamingData ?? {};
  const adaptiveFormats = streamingData?.adaptive_formats ?? streamingData?.adaptiveFormats ?? [];

  if (!Array.isArray(adaptiveFormats)) {
    return [];
  }

  return adaptiveFormats
    .filter((format: any) => {
      const mime = format?.mime_type ?? format?.mimeType ?? "";
      return typeof mime === "string" && mime.includes("audio/");
    })
    .sort((left: any, right: any) => {
      const leftBitrate = Number(left?.bitrate ?? 0);
      const rightBitrate = Number(right?.bitrate ?? 0);
      return rightBitrate - leftBitrate;
    });
};

const extractOnce = async (videoId: string): Promise<ExtractResponse> => {
  const client = await getClient();
  const info = (await client.getInfo(videoId)) as any;

  const basicInfo = (info?.basic_info ?? info?.basicInfo ?? {}) as Record<string, unknown>;
  const title = (basicInfo.title as string | undefined) ?? videoId;
  const channel = (basicInfo.author as string | undefined) ?? null;
  const duration = parseDuration(basicInfo);
  const thumbnail = pickBestThumbnail((basicInfo as { thumbnail?: unknown }).thumbnail);

  const formats = getSortedAudioFormats(info);
  if (formats.length === 0) {
    throw new ServiceError(404, "No audio-only formats found for this video");
  }

  const candidates = formats.slice(0, 8);
  for (const format of candidates) {
    const audioUrl = await resolveAudioUrl(format, client);
    if (audioUrl) {
      return {
        videoId,
        title,
        duration,
        thumbnail,
        channel,
        audioUrl,
        source: "youtubei",
        cached: false
      };
    }
  }

  throw new ServiceError(502, "Unable to resolve playable audio URL from youtubei response");
};

export const extractAudio = async (videoId: string): Promise<ExtractResponse> => {
  metrics.requestsTotal += 1;

  const cached = cache.get(videoId);
  if (cached) {
    metrics.cacheHitTotal += 1;
    return { ...cached, cached: true };
  }

  return limiter(async () => {
    let lastError: Error | null = null;

    for (let attempt = 1; attempt <= Math.max(1, env.EXTRACTION_RETRIES); attempt += 1) {
      try {
        const extracted = await withTimeout(extractOnce(videoId), env.REQUEST_TIMEOUT_MS);
        cache.set(videoId, extracted);
        metrics.successTotal += 1;
        return extracted;
      } catch (error) {
        lastError = error as Error;
        metrics.retryTotal += 1;
        if (attempt < env.EXTRACTION_RETRIES) {
          await sleep(300 * attempt);
        }
      }
    }

    metrics.failureTotal += 1;
    metrics.lastError = (lastError?.message ?? "unknown extraction error").slice(0, 300);

    if (lastError instanceof ServiceError) {
      throw lastError;
    }

    throw new ServiceError(502, `youtubei extraction failed: ${metrics.lastError}`);
  });
};

export const getExtractorMetrics = () => ({
  ...metrics,
  cache: cache.stats()
});
