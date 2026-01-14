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
    videoId: str
    format: str = "mp3"


@app.get("/")
def root():
    return {"status": "local downloader running"}


@app.post("/download")
async def download(data: DownloadIn):
    async with download_semaphore:
        # Build URL directly from videoId (avoids search-triggered bot detection)
        url = f"https://music.youtube.com/watch?v={data.videoId}"

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

        # Prefer m4a when available, otherwise bestaudio/best
        format_selector = "bestaudio[ext=m4a]/bestaudio/best"
        
        # Build command with proper authentication
        cmd = [
            "yt-dlp",
            "-f", format_selector,
            "-x",
            "--audio-format", data.format,
            "-o", output_template,
            "--extractor-args", "youtube:player_client=web",
            "--socket-timeout", "30",
            url
        ]

        # Use cookies for authentication if available
        if use_cookies:
            cmd.extend(["--cookies", cookies_path])
        else:
            # If no cookies, try to extract from browser
            cmd.extend(["--cookies-from-browser", "chrome:auto"])

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode(errors="ignore")
            # If auth/bot error, retry without browser cookies
            if ("Sign in to confirm" in error_msg or "bot" in error_msg.lower()) and not use_cookies:
                cmd = [
                    "yt-dlp",
                    "-f", format_selector,
                    "-x",
                    "--audio-format", data.format,
                    "-o", output_template,
                    "--extractor-args", "youtube:player_client=web",
                    "--extractor-args", "youtube:sts=",
                    "--socket-timeout", "30",
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
            else:
                raise HTTPException(500, error_msg)

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
        # Try get_home first - returns featured playlists and trending content
        home_data = ytmusic.get_home()
    except Exception as exc:
        raise HTTPException(500, f"Failed to fetch home data: {exc}")

    top = []
    
    # Extract tracks from various sections in home data
    if isinstance(home_data, list):
        for section in home_data:
            if not isinstance(section, dict):
                continue
            
            # Look for playlist or chart section
            contents = section.get("contents", [])
            if not contents:
                continue
            
            for item in contents:
                if not isinstance(item, dict):
                    continue
                
                video_id = item.get("videoId")
                if not video_id:
                    continue
                
                artists = ", ".join(
                    [artist.get("name") for artist in item.get("artists", []) if artist.get("name")]
                )
                thumbnails = item.get("thumbnails") or []
                cover = thumbnails[-1].get("url") if thumbnails else None
                
                top.append({
                    "rank": len(top) + 1,
                    "songName": item.get("title"),
                    "singer": artists,
                    "coverPageUrl": cover,
                    "videoId": video_id
                })
                
                # Stop at 10 songs
                if len(top) >= 10:
                    break
            
            if len(top) >= 10:
                break
    
    # Fallback: search for trending Indian songs if home data didn't work
    if not top:
        try:
            results = ytmusic.search("trending india songs", filter="songs", limit=10)
            for idx, item in enumerate(results[:10], start=1):
                video_id = item.get("videoId")
                if not video_id:
                    continue
                    
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
                    "videoId": video_id
                })
        except Exception:
            pass
    
    if not top:
        raise HTTPException(404, "No chart data found")

    return {"tracks": top}

#icon, song name, singers