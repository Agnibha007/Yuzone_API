Yuzone_API — extraction and ffmpeg requirements

This project uses a two-service extraction architecture:

- FastAPI API service for search, queueing, and MP3 generation.
- Node.js `youtubei.js` microservice for primary YouTube audio URL extraction.

FFmpeg is required for audio conversion to MP3 and must be available on PATH.

Quick verification

Open a terminal and run:

```powershell
ffmpeg -version
```

If `ffmpeg` is installed and on PATH you'll see version output. If you see "command not found" or a similar error, follow the instructions below.

Windows (recommended)

- Option A — Chocolatey (recommended if you have Chocolatey):

```powershell
choco install ffmpeg -y
```

- Option B — Download static build:
  1. Visit https://www.gyan.dev/ffmpeg/builds/ or https://www.ffmpeg.org/download.html
  2. Download the static Windows build (zip).
  3. Extract the `ffmpeg` folder and place the `bin` directory somewhere (for example `C:\ffmpeg\bin`).
  4. Add the `bin` folder to your PATH:

```powershell
setx PATH "$($env:PATH);C:\ffmpeg\bin"
# Restart your terminal to pick up the PATH change
```

macOS

```bash
# Using Homebrew
brew install ffmpeg
```

Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install ffmpeg -y
```

Notes

- After installing, re-open your terminal or restart your system shell so PATH changes take effect.
- The FastAPI downloader uses the youtubei microservice as primary extractor and falls back to RapidAPI/pytube/yt-dlp if needed.
- If you'd like, I can add an automated helper to download a static ffmpeg build into the project at runtime (platform-specific). Let me know if you want that option.

Deploying to Northflank

- Build the root [Dockerfile](Dockerfile). It uses Python 3.12, installs FFmpeg, runs as a non-root user, and starts Uvicorn with `HOST` and `PORT` from the environment.
- Use [NORTHFLANK_DEPLOYMENT.md](NORTHFLANK_DEPLOYMENT.md) for the full deployment checklist, health checks, scaling guidance, and persistent volume recommendations.
- Copy [.env.example](.env.example) into Northflank environment variables and mount a persistent volume at `/app/downloads` for cache reuse.
- The optional Node extractor in `services/youtubei-service` can be deployed as a separate service and wired with `YOUTUBEI_SERVICE_URL`.
- The root endpoint `/` redirects to `/top`.
- `GET /health` verifies the FastAPI process is running.
- `GET /ready` verifies FFmpeg, yt-dlp, and cache writability.
- `GET /debug/system` returns Python, platform, FFmpeg, yt-dlp, and cache diagnostics.
- `GET /debug/ytdlp` runs a safe yt-dlp metadata extraction probe.
- `GET /health/download` includes queue metrics and youtubei service health.
- `GET /top` returns the current top 10 songs in India.
- `GET /search?q=` returns title, artists, duration, thumbnail, and videoId.
- `POST /download` and `POST /download/direct` accept a `videoId` and queue asynchronous MP3 download jobs.
- `GET /download/jobs/{jobId}` returns job status and `GET /download/file/{jobId}` returns the MP3 when ready.
- `GET /health/download` and `GET /metrics/download` expose queue/worker health and provider metrics.
- If YouTube blocks requests, add exported cookies to [cookies.txt](cookies.txt) or mount secret cookies at `/etc/secrets/cookies.txt`.
