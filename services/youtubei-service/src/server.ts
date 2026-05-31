import Fastify from "fastify";
import { env } from "./config/env.js";
import { registerExtractRoutes } from "./routes/extract.routes.js";

const app = Fastify({
  logger: {
    level: env.LOG_LEVEL
  }
});

app.register(registerExtractRoutes);

app.setErrorHandler((error, _request, reply) => {
  app.log.error({ err: error }, "unhandled request error");
  reply.status(500).send({ error: "Internal server error" });
});

const start = async () => {
  try {
    await app.listen({
      host: "0.0.0.0",
      port: env.PORT
    });
    app.log.info({ port: env.PORT }, "youtubei service started");
  } catch (error) {
    app.log.error({ err: error }, "failed to start youtubei service");
    process.exit(1);
  }
};

void start();
