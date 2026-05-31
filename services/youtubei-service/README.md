# youtubei-service

TypeScript Fastify microservice that extracts playable audio URLs from YouTube via youtubei.js.

## Endpoints

- `GET /health`
- `GET /metrics`
- `GET /extract/:videoId`
- `POST /extract` with `{ "videoId": "..." }`

## Environment Variables

- `PORT` default `3001`
- `LOG_LEVEL` default `info`
- `CACHE_TTL_SECONDS` default `300`
- `REQUEST_TIMEOUT_MS` default `15000`
- `EXTRACTION_RETRIES` default `3`
- `EXTRACTION_CONCURRENCY` default `4`

## Local Development

```bash
npm install
npm run dev
```

## Build and Run

```bash
npm run build
npm run start
```

## Docker

```bash
docker build -t youtubei-service .
docker run --rm -p 3001:3001 youtubei-service
```
