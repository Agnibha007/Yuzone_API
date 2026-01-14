from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic
import subprocess
import os
import tempfile
import asyncio
from yt_dlp import YoutubeDL

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
        url = f"https://www.youtube.com/watch?v={data.videoId}"
        tmpdir = tempfile.mkdtemp(prefix="dl_")
        
        try:
            # Use YoutubeDL library directly without subprocess - optimized for speed
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': data.format,
                    'preferredquality': '192',
                }],
                'outtmpl': os.path.join(tmpdir, '%(id)s'),
                'quiet': True,
                'no_warnings': True,
                'socket_timeout': 30,
                'retries': 3,
                'fragment_retries': 3,
                'concurrent_fragment_downloads': 4,  # Parallel fragment downloads
                'extractor_args': {'youtube': {'player_client': 'web'}},
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                },
                'encoding': 'utf-8',
                'progress_hooks': [],  # Disable progress hooks for speed
            }
            
            def download_sync():
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        return info.get('id'), info.get('title')
                except Exception as e:
                    raise e
            
            # Run in executor to avoid blocking
            loop = asyncio.get_event_loop()
            video_id, title = await loop.run_in_executor(None, download_sync)
            
            # Find the downloaded file using video ID
            files = [
                f for f in os.listdir(tmpdir)
                if f.endswith(f".{data.format}") and f.startswith(video_id)
            ]
            
            if not files:
                raise HTTPException(500, "Audio file not created")
            
            file_path = os.path.join(tmpdir, files[0])
            # Use title if available, otherwise use filename
            filename = f"{title}.{data.format}" if title else files[0]
            # Sanitize filename for safe download
            filename = "".join(c for c in filename if ord(c) < 128 or c in ' -_.')
            if not filename or filename.startswith('.'):
                filename = files[0]
            
            def file_stream():
                try:
                    with open(file_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)  # Larger buffer = faster streaming
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
        except Exception as e:
            raise HTTPException(500, f"Download failed: {str(e)}")
    
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