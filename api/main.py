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

        cmd = [
            "yt-dlp",
            "-f", "bestaudio/best",
            "--extractor-args", "youtube:player_client=android",
            "-x",
            "--audio-format", data.format,
            "-o", output_template,
            url
        ]

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
            "thumbnail": item.get("thumbnails", [{}])[-1].get("url")
        })

    return formatted_results



#icon, song name, singers