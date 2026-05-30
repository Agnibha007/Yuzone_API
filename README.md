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

- This repo includes [render.yaml](render.yaml) so you can click "New +" → "Blueprint" in the Render dashboard and point it at this repository.
- Render installs ffmpeg during the build, installs the Python dependencies, and starts the API with `uvicorn api.main:app --host 0.0.0.0 --port $PORT --proxy-headers`.
- The blueprint uses `/health` for health checks, so Render can verify the service without relying on a redirect.
- Set any required environment variables in the Render dashboard before the first deploy, including `RAPIDAPI_KEY`, `GITHUB_WEBHOOK_SECRET`, and any Spotify credentials you use.
- The root endpoint `/` redirects to `/top`.
- `GET /health` returns `{"status": "ok"}`.
- `GET /top` returns the current top 10 songs in India.
- `GET /search?q=` returns title, artists, duration, thumbnail, and videoId.
- `POST /download` and `POST /download/direct` accept a `videoId` and return MP3 audio.
- If YouTube blocks requests, add exported cookies to [cookies.txt](cookies.txt) in Netscape HTTP Cookie File format; Render will pick them up during build.
