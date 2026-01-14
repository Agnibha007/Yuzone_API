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
        
        # Check cache first
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
        
        # Perform direct download on server (works on localhost)
        tmpdir = tempfile.mkdtemp(prefix="dl_")
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        try:
            from yt_dlp import YoutubeDL
            
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': format_ext,
                    'preferredquality': '192',
                }],
                'outtmpl': os.path.join(tmpdir, 'audio'),
                'quiet': True,
                'no_warnings': True,
                'socket_timeout': 30,
            }
            
            def download_sync():
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return info.get('title', video_id)
            
            loop = asyncio.get_event_loop()
            title = await loop.run_in_executor(None, download_sync)
            
            # Find downloaded file
            files = [f for f in os.listdir(tmpdir) if f.endswith(f".{format_ext}")]
            
            if not files:
                raise HTTPException(500, "Audio file not created")
            
            file_path = os.path.join(tmpdir, files[0])
            filename = f"{title}.{format_ext}" if title else files[0]
            filename = "".join(c for c in filename if ord(c) < 128 or c in ' -_.')
            
            # Cache the file for future requests
            try:
                import shutil
                cached_path = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
                shutil.copy2(file_path, cached_path)
            except:
                pass
            
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
            
        except Exception as e:
            raise HTTPException(500, f"Download failed: {str(e)}")


@app.post("/download/direct")
async def download_direct(data: DownloadIn):
    """
    Direct download using external APIs that bypass YouTube bot detection.
    Works on Render without IP blocking issues.
    """
    video_id = data.videoId
    format_ext = data.format
    
    # Check cache first
    cached_file = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
    if os.path.exists(cached_file):
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
            headers={"Content-Disposition": f'attachment; filename="{video_id}.{format_ext}"'}
        )
    
    # Use external APIs to download (no direct YouTube access)
    import httpx
    tmpdir = tempfile.mkdtemp(prefix="dl_")
    
    # Try Method 1: Cobalt API (reliable, free, no auth needed)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.cobalt.tools/api/json",
                json={
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "vCodec": "h264",
                    "vQuality": "720",
                    "aFormat": "mp3" if format_ext == "mp3" else "best",
                    "isAudioOnly": True
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                
                if result.get("status") == "stream" or result.get("status") == "redirect":
                    audio_url = result.get("url")
                    
                    if audio_url:
                        # Download the audio file
                        audio_response = await client.get(audio_url)
                        
                        if audio_response.status_code == 200:
                            # Save to temp file
                            temp_file = os.path.join(tmpdir, f"{video_id}.{format_ext}")
                            with open(temp_file, "wb") as f:
                                f.write(audio_response.content)
                            
                            # Cache it
                            try:
                                import shutil
                                shutil.copy2(temp_file, cached_file)
                            except:
                                pass
                            
                            # Stream response
                            def file_stream():
                                try:
                                    with open(temp_file, "rb") as f:
                                        while True:
                                            chunk = f.read(65536)
                                            if not chunk:
                                                break
                                            yield chunk
                                finally:
                                    try:
                                        os.remove(temp_file)
                                        os.rmdir(tmpdir)
                                    except:
                                        pass
                            
                            return StreamingResponse(
                                file_stream(),
                                media_type="audio/mpeg",
                                headers={"Content-Disposition": f'attachment; filename="{video_id}.{format_ext}"'}
                            )
    except Exception as e:
        print(f"Cobalt API failed: {e}")
    
    # Try Method 2: Y2Mate API alternative (loader.to)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Get download links
            response = await client.get(
                f"https://ab.cococococ.com/ajax/download.php",
                params={
                    "copyright": "0",
                    "format": "mp3" if format_ext == "mp3" else "m4a",
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "api": "dfcb6d76f2f6a9894gjkege8a4ab232222"
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                
                if result.get("success"):
                    download_url = result.get("url")
                    
                    if download_url:
                        audio_response = await client.get(download_url)
                        
                        if audio_response.status_code == 200:
                            temp_file = os.path.join(tmpdir, f"{video_id}.{format_ext}")
                            with open(temp_file, "wb") as f:
                                f.write(audio_response.content)
                            
                            # Cache it
                            try:
                                import shutil
                                shutil.copy2(temp_file, cached_file)
                            except:
                                pass
                            
                            def file_stream():
                                try:
                                    with open(temp_file, "rb") as f:
                                        while True:
                                            chunk = f.read(65536)
                                            if not chunk:
                                                break
                                            yield chunk
                                finally:
                                    try:
                                        os.remove(temp_file)
                                        os.rmdir(tmpdir)
                                    except:
                                        pass
                            
                            return StreamingResponse(
                                file_stream(),
                                media_type="audio/mpeg",
                                headers={"Content-Disposition": f'attachment; filename="{video_id}.{format_ext}"'}
                            )
    except Exception as e:
        print(f"Y2Mate alternative failed: {e}")
    
    # Try Method 3: Local yt-dlp (works on localhost but may fail on Render)
    try:
        from yt_dlp import YoutubeDL
        
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': format_ext,
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(tmpdir, 'audio'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
        }
        
        def download_sync():
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
                return info.get('title', video_id)
        
        loop = asyncio.get_event_loop()
        title = await loop.run_in_executor(None, download_sync)
        
        files = [f for f in os.listdir(tmpdir) if f.endswith(f".{format_ext}")]
        
        if files:
            file_path = os.path.join(tmpdir, files[0])
            filename = f"{title}.{format_ext}" if title else files[0]
            filename = "".join(c for c in filename if ord(c) < 128 or c in ' -_.')
            
            # Cache
            try:
                import shutil
                shutil.copy2(file_path, cached_file)
            except:
                pass
            
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
    except Exception as e:
        print(f"yt-dlp fallback failed: {e}")
    
    raise HTTPException(503, "All download methods failed. YouTube may be blocking requests. Try again later or pre-cache files.")


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