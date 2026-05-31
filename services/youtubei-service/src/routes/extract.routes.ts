import type { FastifyInstance } from "fastify";
import { extractByBody, extractByParam } from "../controllers/extract.controller.js";
import { getExtractorMetrics } from "../services/youtubei.service.js";

export const registerExtractRoutes = async (app: FastifyInstance) => {
  app.get("/health", async () => ({
    status: "ok",
    service: "youtubei-service",
    uptimeSeconds: Math.round(process.uptime())
  }));

  app.get("/metrics", async () => ({
    status: "ok",
    metrics: getExtractorMetrics()
  }));

  app.get("/extract/:videoId", extractByParam);
  app.post("/extract", extractByBody);
};
