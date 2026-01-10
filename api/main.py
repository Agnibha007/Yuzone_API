from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic
import subprocess
import os
import glob

app = FastAPI()
ytmusic = YTMusic()

DOWNLOAD_DIR = os.path.abspath("downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class DownloadIn(BaseModel):
    query: str
    format: str = "mp3"


@app.get("/")
def root():
    return {"status": "local downloader running"}


@app.post("/download")
def download(data: DownloadIn):
    # 1. Search song
    results = ytmusic.search(data.query, filter="songs", limit=1)
    if not results:
        raise HTTPException(404, "No song found")

    song = results[0]
    video_id = song.get("videoId")
    if not video_id:
        raise HTTPException(500, "Invalid video ID")

    url = f"https://music.youtube.com/watch?v={video_id}"

    # 2. Download with ytdlp
    output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")

    cmd = [
    "yt-dlp",
    "--cookies", "cookies.txt",
    "-x",
    "--audio-format", data.format,
    "-o", output_template,
    url
]


    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    if result.returncode != 0:
        raise HTTPException(500, result.stderr.decode(errors="ignore"))

    # 3. Find downloaded file
    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"*.{data.format}"))
    if not files:
        raise HTTPException(500, "Audio file not created")

    file_path = files[0]
    filename = os.path.basename(file_path)

    # 4. Stream file and delete after sending
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
            except OSError:
                pass

    return StreamingResponse(
        file_stream(),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )
