from typing import List
from fastapi import FastAPI, status, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import tempfile
import shutil
import os

app = FastAPI()

class SearchRequest(BaseModel):
    query: str


def fetch_ytmp3_results(query: str) -> List[str]:
    try:
        from ytmusicapi import YTMusic

        ytm = YTMusic()
        try:
            items = ytm.search(query, filter="songs", limit=10)
        except TypeError:
            items = ytm.search(query, limit=10)

        results: List[str] = []
        for it in items:
            title = it.get("title") or it.get("videoTitle") or ""
            artists = it.get("artists") or it.get("artist") or []
            if isinstance(artists, list):
                artist_names = ", ".join(a.get("name") for a in artists if isinstance(a, dict) and a.get("name"))
            elif isinstance(artists, str):
                artist_names = artists
            else:
                artist_names = ""
            entry = title
            if artist_names:
                entry = f"{title} - {artist_names}"
            results.append(entry)
        return results
    except Exception:
        return []


def _download_query_to_mp3(query: str, tmpdir: str) -> str:
    ffmpeg_path = "./bin/ffmpeg"
    ffprobe_path = "./bin/ffprobe"

    try:
        from yt_dlp import YoutubeDL
    except Exception as e:
        raise RuntimeError("yt-dlp is not installed") from e

    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "prefer_ffmpeg": True,
        "ffmpeg_location": ffmpeg_path,
        "extractor_args": {
            "youtube": {
                "player_client": ["web"]
            }
        }
    }

    ydl = YoutubeDL(ydl_opts)
    search_url = f"ytsearch1:{query}"
    with ydl:
        info = ydl.extract_info(search_url, download=True)

    for fname in os.listdir(tmpdir):
        if fname.lower().endswith(".mp3"):
            return os.path.join(tmpdir, fname)

    raise RuntimeError("No mp3 file produced by yt-dlp")


@app.post("/musiclist", status_code=status.HTTP_200_OK)
def music_list(search: SearchRequest):
    query = search.query.strip()
    results: List[str] = []
    external = fetch_ytmp3_results(query)

    for item in external:
        if item not in results:
            results.append(item)
    results = results[:10]

    return {"results": results}


@app.post("/getmusic", status_code=status.HTTP_200_OK)
def get_music(search: SearchRequest):

    query = search.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query")

    tmpdir = tempfile.mkdtemp(prefix="yuzone_")
    try:
        mp3_path = _download_query_to_mp3(query, tmpdir)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Download failed: {e}")

    def iterfile(path: str, tmpdir: str):
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    yield chunk
        finally:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

    safe_name = f"{query}.mp3".replace('"', "'")
    headers = {"Content-Disposition": f'attachment; filename="{safe_name}"'}
    return StreamingResponse(iterfile(mp3_path, tmpdir), media_type="audio/mpeg", headers=headers)
