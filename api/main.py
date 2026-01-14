from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic
import subprocess
import os
import tempfile
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

app = FastAPI()
ytmusic = YTMusic()

# allow at most 5 downloads running at the same time
download_semaphore = asyncio.Semaphore(5)

# Setup cache directory
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "downloads")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MANIFEST = os.path.join(CACHE_DIR, "manifest.json")


class DownloadIn(BaseModel):
    videoId: str
    format: str = "mp3"


@app.get("/")
def root():
    return {"status": "local downloader running"}


@app.post("/download")
async def download(data: DownloadIn):
    async with download_semaphore:
        video_id = data.videoId
        format_ext = data.format
        
        # Check cache first - FASTEST option for Render
        cached_file = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
        if os.path.exists(cached_file):
            filename = f"{video_id}.{format_ext}"
            
            def cached_stream():
                with open(cached_file, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            
            return StreamingResponse(
                cached_stream(),
                media_type="audio/mpeg",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'}
            )
        
        # Not in cache - try to download
        tmpdir = tempfile.mkdtemp(prefix="dl_")
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        try:
            # Try method 1: pytube (works better on cloud servers)
            try:
                from pytube import YouTube
                yt = YouTube(url)
                stream = yt.streams.filter(only_audio=True).first()
                if stream:
                    temp_file = os.path.join(tmpdir, f"audio.{stream.default_audio_codec}")
                    stream.download(output_path=tmpdir, filename=f"audio.{stream.default_audio_codec}")
                    
                    # Convert if needed
                    if stream.default_audio_codec != format_ext:
                        converted = await convert_audio(temp_file, format_ext, tmpdir)
                        temp_file = converted
                    
                    title = yt.title or video_id
                    await cache_file(temp_file, video_id, format_ext)
                    return await stream_file(temp_file, f"{title}.{format_ext}", tmpdir)
            except Exception as e:
                print(f"pytube failed: {e}")
            
            # Try method 2: yt-dlp with simpler options
            from yt_dlp import YoutubeDL
            ydl_opts = {
                'format': 'bestaudio',
                'outtmpl': os.path.join(tmpdir, '%(id)s'),
                'quiet': True,
                'no_warnings': True,
                'extractor_args': {'youtube': {'player_client': 'web'}},
                'socket_timeout': 30,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                },
            }
            
            def download_sync():
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return info.get('id'), info.get('title')
            
            loop = asyncio.get_event_loop()
            vid_id, title = await loop.run_in_executor(None, download_sync)
            
            # Find downloaded file
            files = [f for f in os.listdir(tmpdir) if not f.startswith('.')]
            if not files:
                raise Exception("No audio file downloaded")
            
            audio_file = os.path.join(tmpdir, files[0])
            
            # Convert to desired format if needed
            if not audio_file.endswith(f".{format_ext}"):
                audio_file = await convert_audio(audio_file, format_ext, tmpdir)
            
            # Cache the file
            await cache_file(audio_file, video_id, format_ext)
            
            filename = f"{title}.{format_ext}" if title else f"{video_id}.{format_ext}"
            filename = "".join(c for c in filename if ord(c) < 128 or c in ' -_.')
            
            return await stream_file(audio_file, filename, tmpdir)
            
        except Exception as e:
            error_msg = str(e)
            # Return cached version if download fails on Render
            if "Sign in" in error_msg or "bot" in error_msg.lower():
                raise HTTPException(503, "YouTube blocking downloads from this server. Please try again later.")
            raise HTTPException(500, f"Download failed: {error_msg}")


async def convert_audio(input_file, output_format, tmpdir):
    """Convert audio file to desired format using ffmpeg"""
    output_file = os.path.join(tmpdir, f"converted.{output_format}")
    cmd = [
        "ffmpeg", "-i", input_file, "-q:a", "0", "-map", "a", output_file, "-y"
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    
    if os.path.exists(output_file):
        try:
            os.remove(input_file)
        except:
            pass
        return output_file
    return input_file


async def cache_file(file_path, video_id, format_ext):
    """Cache downloaded file"""
    cached_path = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
    try:
        import shutil
        shutil.copy2(file_path, cached_path)
        update_manifest(video_id, format_ext)
    except Exception as e:
        print(f"Cache error: {e}")


def update_manifest(video_id, format_ext):
    """Update cache manifest"""
    try:
        manifest = {}
        if os.path.exists(CACHE_MANIFEST):
            with open(CACHE_MANIFEST, "r") as f:
                manifest = json.load(f)
        
        manifest[video_id] = {
            "format": format_ext,
            "cached_at": datetime.now().isoformat()
        }
        
        with open(CACHE_MANIFEST, "w") as f:
            json.dump(manifest, f)
    except Exception as e:
        print(f"Manifest error: {e}")


async def stream_file(file_path, filename, tmpdir):
    """Stream file and cleanup"""
    def file_stream():
        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(file_path)
                os.rmdir(tmpdir)
            except:
                pass
    
    return StreamingResponse(
        file_stream(),
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
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