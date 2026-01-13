from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic
import subprocess
import os
import tempfile
import asyncio
import sys
import urllib.parse

app = FastAPI()
ytmusic = YTMusic()

# allow at most 5 downloads running at the same time
download_semaphore = asyncio.Semaphore(5)

# Windows asyncio fix: enable subprocess support
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class DownloadIn(BaseModel):
    videoId: str
    format: str = "mp3"


@app.get("/")
def root():
    return {"status": "local downloader running"}


@app.post("/download")
async def download(data: DownloadIn):
    async with download_semaphore:
        # 1. Validate & build URL from provided videoId
        video_id = (data.videoId or "").strip()
        if not video_id:
            raise HTTPException(400, "videoId is required")

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

        # Use thread-based subprocess on Windows-compatible event loops
        def run_dl():
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        process = await asyncio.to_thread(run_dl)
        if process.returncode != 0:
            raise HTTPException(500, process.stderr.decode(errors="ignore"))

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

        # Build ASCII-safe Content-Disposition with RFC 5987 filename*
        name_only, ext = os.path.splitext(filename)
        safe_ascii_name = "".join(c if (ord(c) < 128 and c not in [';', '"']) else '_' for c in name_only) or "download"
        safe_ascii = safe_ascii_name + ext
        encoded = urllib.parse.quote(filename)
        content_disp = f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{encoded}"

        return StreamingResponse(
            file_stream(),
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": content_disp
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

    # YTMusic may return different keys/use nested structures across versions/regions.
    candidate_lists = []
    for key in ["tracks", "songs", "topSongs", "trending", "hotlist"]:
        val = charts.get(key)
        if isinstance(val, list):
            candidate_lists.append(val)
        elif isinstance(val, dict):
            if isinstance(val.get("results"), list):
                candidate_lists.append(val["results"])
            elif isinstance(val.get("items"), list):
                candidate_lists.append(val["items"])

    # pick first non-empty candidate list
    tracks = next((lst for lst in candidate_lists if lst), [])
    if not tracks:
        # Fallback: try popular India playlists and return their first 10 tracks
        playlist_queries = [
            "Top 100 India",
            "India Top Hits",
            "Bollywood Top Hits",
            "Trending India",
            "Top Songs India"
        ]

        playlist_id = None
        for q in playlist_queries:
            try:
                plist_results = ytmusic.search(q, filter="playlists", limit=5)
            except Exception:
                plist_results = []

            # Pick the first reasonable match that mentions India/Top/Trending
            for r in plist_results:
                title = (r.get("title") or "").lower()
                if ("india" in title and ("top" in title or "trending" in title or "hits" in title)) or "bollywood" in title:
                    playlist_id = r.get("playlistId") or r.get("browseId")
                    if playlist_id:
                        break
            if playlist_id:
                break

        if playlist_id:
            try:
                playlist = ytmusic.get_playlist(playlist_id, limit=50)
                tracks = playlist.get("tracks") or []
            except Exception:
                tracks = []

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