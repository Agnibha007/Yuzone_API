from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic
import subprocess
import os
import tempfile
import asyncio

app = FastAPI()
ytmusic = YTMusic()

# allow at most 5 downloads running at the same time
download_semaphore = asyncio.Semaphore(5)


class DownloadIn(BaseModel):
    query: str
    format: str = "mp3"


@app.get("/")
def root():
    return {"status": "local downloader running"}


@app.post("/download")
async def download(data: DownloadIn):
    async with download_semaphore:
        # 1. Search song
        results = ytmusic.search(data.query, filter="songs", limit=1)
        if not results:
            raise HTTPException(404, "No song found")

        song = results[0]
        video_id = song.get("videoId")
        if not video_id:
            raise HTTPException(500, "Invalid video ID")

        url = f"https://music.youtube.com/watch?v={video_id}"

        # 2. Isolated temp directory (critical)
        tmpdir = tempfile.mkdtemp(prefix="dl_")
        output_template = os.path.join(tmpdir, "%(title)s.%(ext)s")

        # Optional: pass cookies if available to avoid bot verification/age/country blocks
        cookies_path = os.path.join(os.path.dirname(__file__), "..", "cookies.txt")
        use_cookies = False
        if os.path.exists(cookies_path):
            try:
                with open(cookies_path, "r", encoding="utf-8", errors="ignore") as fh:
                    head = fh.read(200)
                    # yt-dlp expects Netscape HTTP Cookie File format; skip if not
                    use_cookies = "Netscape HTTP Cookie File" in head
            except OSError:
                use_cookies = False

        # Prefer m4a when available, otherwise bestaudio/best. Avoid android client when cookies are used.
        format_selector = "bestaudio[ext=m4a]/bestaudio/best"
        cmd = [
            "yt-dlp",
            "-f", format_selector,
            "-x",
            "--audio-format", data.format,
            "-o", output_template,
            url
        ]

        # When not using cookies, keep the android player client which often works without auth.
        if not use_cookies:
            cmd.extend(["--extractor-args", "youtube:player_client=android"])

        if use_cookies:
            cmd.extend(["--cookies", cookies_path])

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise HTTPException(500, stderr.decode(errors="ignore"))

        # 3. Locate the downloaded file
        files = [
            f for f in os.listdir(tmpdir)
            if f.endswith(f".{data.format}")
        ]

        if not files:
            raise HTTPException(500, "Audio file not created")

        file_path = os.path.join(tmpdir, files[0])
        filename = files[0]

        # 4. Stream & cleanup (per-request safe)
        def file_stream():
            try:
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(8192)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    os.remove(file_path)
                    os.rmdir(tmpdir)
                except OSError:
                    pass

        return StreamingResponse(
            file_stream(),
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    
@app.get("/search")
def search(q: str):
    results = ytmusic.search(q, filter="songs", limit=20)

    if not results:
        raise HTTPException(404, "No results found")

    formatted_results = []
    for item in results:
        formatted_results.append({
            "title": item.get("title"),
            "artists": [artist.get("name") for artist in item.get("artists", [])],
            "duration": item.get("duration"),
            "thumbnail": item.get("thumbnails", [{}])[-1].get("url"),
            "videoId": item.get("videoId")
        })

    return formatted_results



@app.get("/top")
def top_songs():
    try:
        charts = ytmusic.get_charts(country="IN")
    except Exception as exc:  # network or API errors
        raise HTTPException(500, f"Failed to fetch charts: {exc}")

    tracks = charts.get("tracks") or []
    if not tracks:
        raise HTTPException(404, "No chart data found")

    top = []
    for idx, item in enumerate(tracks[:10], start=1):
        artists = ", ".join(
            [artist.get("name") for artist in item.get("artists", []) if artist.get("name")]
        )
        thumbnails = item.get("thumbnails") or []
        cover = thumbnails[-1].get("url") if thumbnails else None

        top.append({
            "rank": idx,
            "songName": item.get("title"),
            "singer": artists,
            "coverPageUrl": cover,
            "videoId": item.get("videoId")
        })

    return {"tracks": top}

#icon, song name, singers