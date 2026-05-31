import type { FastifyReply, FastifyRequest } from "fastify";
import { z } from "zod";
import { extractAudio } from "../services/youtubei.service.js";
import { ServiceError } from "../utils/errors.js";

const bodySchema = z.object({
  videoId: z.string().min(6)
});

const paramsSchema = z.object({
  videoId: z.string().min(6)
});

const normalizeError = (error: unknown) => {
  if (error instanceof ServiceError) {
    return { statusCode: error.statusCode, message: error.message };
  }

  const message = error instanceof Error ? error.message : "Unknown extractor error";
  return { statusCode: 500, message };
};

export const extractByParam = async (
  request: FastifyRequest<{ Params: { videoId: string } }>,
  reply: FastifyReply
) => {
  const parsed = paramsSchema.safeParse(request.params);
  if (!parsed.success) {
    return reply.status(400).send({ error: "Invalid videoId" });
  }

  try {
    const payload = await extractAudio(parsed.data.videoId);
    return reply.send(payload);
  } catch (error) {
    const normalized = normalizeError(error);
    return reply.status(normalized.statusCode).send({ error: normalized.message });
  }
};

export const extractByBody = async (
  request: FastifyRequest<{ Body: { videoId: string } }>,
  reply: FastifyReply
) => {
  const parsed = bodySchema.safeParse(request.body);
  if (!parsed.success) {
    return reply.status(400).send({ error: "Invalid request body" });
  }

  try {
    const payload = await extractAudio(parsed.data.videoId);
    return reply.send(payload);
  } catch (error) {
    const normalized = normalizeError(error);
    return reply.status(normalized.statusCode).send({ error: normalized.message });
  }
};
