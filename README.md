Yuzone_API — ffmpeg requirement

This project uses `yt-dlp` to download and convert YouTube audio to MP3. That conversion requires `ffmpeg` to be installed and available on your system PATH.

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
- `yt-dlp` relies on ffmpeg for audio extraction/encoding. If ffmpeg is not available, the `/getmusic` endpoint will return an error indicating ffmpeg is missing.
- If you'd like, I can add an automated helper to download a static ffmpeg build into the project at runtime (platform-specific). Let me know if you want that option.

Deploying to Render

- This repo includes [render.yaml](render.yaml) so you can click "New +" → "Blueprint" in the Render dashboard and point to this repository.
- Build installs ffmpeg (`apt-get install -y ffmpeg`) and the Python deps, and starts with `uvicorn api.main:app --host 0.0.0.0 --port $PORT`.
- Make sure the repo is public or connect your Git provider so Render can pull it; auto-deploy is enabled in the blueprint.
- Health check: the root endpoint `/` returns `{"status": "local downloader running"}` once the service is up.
- Top charts: `/top` returns the current top 10 songs in India (rank, title, singer, cover art, videoId).
  - If public charts are unavailable in your region, the API falls back to a popular India playlist (Top/Hits/Trending) and returns the first 10 tracks from it.
- Search: `/search?q=` returns title, artists, duration, thumbnail, videoId.
- Download: `/download` accepts `{ "videoId": "YOUTUBE_VIDEO_ID", "format": "mp3" }`. If you hit YouTube bot/age prompts, add exported cookies to [cookies.txt](cookies.txt) (Render will read it automatically during build). The file must be in "Netscape HTTP Cookie File" format (the default when exporting via the yt-dlp FAQ instructions); otherwise cookies are ignored.

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"videoId":"YALvuUpY_b0","format":"mp3"}' \
  https://<your-render-service>/download
```
