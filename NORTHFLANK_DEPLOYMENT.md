# Northflank Deployment

## Architecture Review

The repository contains a FastAPI API service and an optional Node.js `youtubei.js` extractor service. The API owns search, queueing, MP3 generation, cache reuse, and fallback providers. The extractor service is useful as the first YouTube extraction path, but the API now starts without it and falls back to RapidAPI, pytube, and yt-dlp.

## Production Service

Create a Northflank service from the repository root and select Dockerfile build.

- Dockerfile: `Dockerfile`
- Port: `8080`
- Health check path: `/health`
- Readiness check path: `/ready`
- Persistent volume mount: `/app/downloads`
- Runtime user: non-root `appuser`

Northflank injects `PORT`; the container command reads `HOST` and `PORT` from the environment and binds to `0.0.0.0`.

## Recommended Environment Variables

Use `.env.example` as the source of truth. For production, start with:

```text
HOST=0.0.0.0
PORT=8080
LOG_LEVEL=INFO
CACHE_DIR=/app/downloads
CACHE_RETENTION_SECONDS=86400
CACHE_CLEANUP_INTERVAL_SECONDS=900
CACHE_MAX_BYTES=2147483648
CACHE_CLEANUP_ON_START=false
DOWNLOAD_WORKER_COUNT=2
DOWNLOAD_QUEUE_MAXSIZE=200
DOWNLOAD_MAX_RETRIES=4
DOWNLOAD_PROVIDER_TIMEOUT_SECONDS=120
DOWNLOAD_UPSTREAM_CONCURRENCY=2
DOWNLOAD_RATE_LIMIT_MAX_REQUESTS=30
ENABLE_DEPLOY_WEBHOOK=false
```

Optional:

- `YOUTUBEI_SERVICE_URL`: URL of the Node extractor service.
- `RAPIDAPI_KEY`: paid fallback extractor key.
- Mount cookies as a secret file at `/etc/secrets/cookies.txt` if YouTube requires cookies.

## Health Checks

- `/health`: process liveness only.
- `/ready`: verifies FFmpeg, yt-dlp, and cache writability.
- `/debug/system`: dependency and platform diagnostics.
- `/debug/ytdlp`: safe yt-dlp metadata extraction test. It does not download media or expose secrets.

Use `/health` for the platform liveness probe. Use `/ready` for deployment verification and alerting.

## Scaling

Start with one API replica and a persistent volume for `/app/downloads`. Increase replicas only if you are comfortable with per-replica in-memory queues and cache coordination. For multi-replica production, move queue state to Redis or a managed task system before scaling write-heavy download traffic.

Recommended initial limits:

- CPU: 1 vCPU minimum.
- Memory: 1 GB minimum, 2 GB preferred for concurrent FFmpeg conversions.
- `DOWNLOAD_UPSTREAM_CONCURRENCY=2`
- `DOWNLOAD_WORKER_COUNT=2`

## Persistent Volume

Mount a volume at `/app/downloads` to preserve MP3 cache between deploys. Size it from expected traffic; set `CACHE_MAX_BYTES` below the volume size so cleanup has room to work.

## Risk Assessment

- YouTube extraction can still be blocked by bot checks, 429s, invalid cookies, or geo/age restrictions. Errors are now classified and returned as JSON.
- The queue is in-memory, so jobs are lost on restart and are not shared across replicas.
- The legacy GitHub deployment webhook is disabled by default because container platforms should deploy from CI/CD, not by running `git pull` inside the service.
- Playlist ZIP downloads are still synchronous and can be expensive for very large playlists; keep request limits conservative.

## Reliability Improvements Ranked by Impact

1. Use the optional youtubei service as the primary extractor and keep yt-dlp as a fallback.
2. Mount fresh YouTube cookies at `/etc/secrets/cookies.txt` when bot checks increase.
3. Keep `DOWNLOAD_UPSTREAM_CONCURRENCY` low to reduce rate limiting.
4. Use persistent cache volume at `/app/downloads`.
5. Watch `/debug/ytdlp`, `/ready`, and structured logs for failure categories.
6. Add Redis-backed queueing before horizontal scaling.

## Final Deployment Checklist

- Build succeeds with Python 3.12 Dockerfile.
- Northflank service exposes port `8080`.
- `/health` returns 200.
- `/ready` returns 200 after FFmpeg, yt-dlp, and cache checks pass.
- Persistent volume is mounted at `/app/downloads`.
- Secrets are configured without committing `cookies.txt`.
- `ENABLE_DEPLOY_WEBHOOK=false`.
- Optional youtubei service URL is reachable, or fallback providers are acceptable.
