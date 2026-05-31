export type ExtractResponse = {
  videoId: string;
  title: string;
  duration: number | null;
  thumbnail: string | null;
  channel: string | null;
  audioUrl: string;
  source: "youtubei";
  cached: boolean;
};

export type ExtractMetrics = {
  requestsTotal: number;
  successTotal: number;
  failureTotal: number;
  retryTotal: number;
  cacheHitTotal: number;
  lastError: string | null;
};
