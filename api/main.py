from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, RedirectResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic
import subprocess
import os
import tempfile
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
import httpx
import hmac
import hashlib
import base64
from urllib.parse import urlparse, parse_qs
from typing import Optional

# Load .local.env if it exists
env_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".local.env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

app = FastAPI()
ytmusic = YTMusic()

# Simple in-memory cache for Spotify client credentials tokens
spotify_token_cache = {
    "token": None,
    "expires_at": 0.0  # unix timestamp
}

# allow at most 5 downloads running at the same time
download_semaphore = asyncio.Semaphore(5)

# Setup cache directory
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "downloads")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MANIFEST = os.path.join(CACHE_DIR, "manifest.json")


class DownloadIn(BaseModel):
    videoId: str
    quality: int = 2  # 1=low, 2=medium, 3=high
    # Format is always MP3 - no other formats supported


class PlaylistDownloadIn(BaseModel):
    videoIds: list
    quality: int = 2  # 1=low, 2=medium, 3=high
    # Format is always MP3 - no other formats supported


def get_quality_settings(quality: int) -> dict:
    """
    Get FFmpeg quality settings based on quality level.
    
    1 = Low quality (low bandwidth, 96 kbps)
    2 = Medium quality (128 kbps) - default
    3 = High quality (320 kbps)
    """
    quality_map = {
        1: {"bitrate": "96k", "vbr": "9"},  # Low quality
        2: {"bitrate": "128k", "vbr": "6"},  # Medium quality
        3: {"bitrate": "320k", "vbr": "0"}   # High quality
    }
    
    # Default to medium if invalid quality value
    if quality not in quality_map:
        quality = 2
    
    return quality_map[quality]


def get_yt_dlp_options(tmpdir: str, bin_dir: str, format_ext: str, quality: int) -> dict:
    """
    Generate optimized yt-dlp options to handle 403 errors and bot detection.
    """
    quality_settings = get_quality_settings(quality)
    cookies_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cookies.txt')
    
    opts = {
        # More flexible format selection - tries audio-only first, then combined formats
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': format_ext,
            'preferredquality': quality_settings['bitrate'],
            'nopostoverwrites': False,
        }],
        'outtmpl': os.path.join(tmpdir, '%(id)s'),
        'quiet': False,
        'no_warnings': False,
        'socket_timeout': 30,
        'ffmpeg_location': bin_dir,
        'keepvideo': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Upgrade-Insecure-Requests': '1',
        },
        'concurrent_fragment_downloads': 4,
        'fragment_retries': 5,
        'file_access_retries': 15,
        'retries': 10,
        'skip_unavailable_fragments': True,
        'nocheckcertificate': True,
        'prefer_insecure': False,
        'allow_unplayable_formats': True,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web', 'mweb'],
                'lang': ['en'],
                'skip': ['dash', 'hls'],
            }
        },
    }
    
    # Add cookies if available - critical for reducing 403 errors
    if os.path.exists(cookies_file):
        opts['cookiefile'] = cookies_file
    
    return opts



@app.get("/")
def root():
    return RedirectResponse(url="/top")


@app.post("/webhook/deploy")
async def github_webhook(request: Request):
    """
    GitHub webhook endpoint for auto-deployment.
    Set this as webhook URL in GitHub repo settings.
    """
    # Optional: Verify GitHub signature (recommended for security)
    webhook_secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if webhook_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        body = await request.body()
        expected_signature = "sha256=" + hmac.new(
            webhook_secret.encode(),
            body,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(signature, expected_signature):
            raise HTTPException(403, "Invalid signature")
    
    payload = await request.json()
    
    # Only respond to push events on main/master branch
    if payload.get("ref") in ["refs/heads/main", "refs/heads/master"]:
        try:
            # Get the repo directory
            repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            
            # Pull latest changes
            result = subprocess.run(
                ["git", "pull", "origin", "master"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                # Restart the systemd service (if using systemd)
                try:
                    subprocess.run(
                        ["sudo", "systemctl", "restart", "yuzone-api"],
                        timeout=10
                    )
                    return {"status": "success", "message": "Pulled changes and restarted service"}
                except Exception as e:
                    return {"status": "partial", "message": f"Pulled changes but restart failed: {e}"}
            else:
                return {"status": "error", "message": result.stderr}
        except Exception as e:
            raise HTTPException(500, f"Deployment failed: {str(e)}")
    
    return {"status": "ignored", "message": "Not a push to master/main"}


@app.post("/download")
async def download(data: DownloadIn):
    async with download_semaphore:
        video_id = data.videoId
        format_ext = "mp3"  # Always MP3 format
        quality = data.quality if hasattr(data, 'quality') else 2
        
        # Validate quality
        if quality not in [1, 2, 3]:
            raise HTTPException(400, "Quality must be 1 (low), 2 (medium), or 3 (high)")
        
        # Check cache first
        cached_file = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
        if os.path.exists(cached_file):
            filename = f"{video_id}.{format_ext}"
            file_size = os.path.getsize(cached_file)
            
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
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes"
                }
            )
        
        # Perform direct download on server (works on localhost)
        tmpdir = tempfile.mkdtemp(prefix="dl_")
        url = f"https://www.youtube.com/watch?v={video_id}"
        
        try:
            from yt_dlp import YoutubeDL
            
            # Get ffmpeg location from bin/ directory
            bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bin')
            
            # Get optimized yt-dlp options
            ydl_opts = get_yt_dlp_options(tmpdir, bin_dir, format_ext, quality)
            
            def download_sync():
                with YoutubeDL(ydl_opts) as ydl:
                    try:
                        info = ydl.extract_info(url, download=True)
                        return info.get('title', video_id)
                    except Exception as e:
                        error_msg = str(e)
                        if '403' in error_msg:
                            raise HTTPException(403, f"YouTube blocked the request (403). Try using the /download/direct endpoint or ensure cookies are set up.")
                        elif '429' in error_msg:
                            raise HTTPException(429, f"Rate limited by YouTube. Please wait before trying again.")
                        else:
                            raise HTTPException(500, f"yt-dlp extraction failed: {error_msg}")
            
            loop = asyncio.get_event_loop()
            title = await loop.run_in_executor(None, download_sync)
            
            # Find downloaded file
            files = [f for f in os.listdir(tmpdir) if f.endswith(f".{format_ext}")]
            
            if not files:
                raise HTTPException(500, "Audio file not created after postprocessing")
            
            file_path = os.path.join(tmpdir, files[0])
            filename = f"{title}.{format_ext}" if title else files[0]
            filename = "".join(c for c in filename if ord(c) < 128 or c in ' -_.')
            
            # Cache the file for future requests
            try:
                import shutil
                cached_path = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
                shutil.copy2(file_path, cached_path)
            except Exception as cache_error:
                # Log but don't fail if caching fails
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
                    except Exception:
                        pass
            
            file_size = os.path.getsize(file_path)
            
            return StreamingResponse(
                file_stream(),
                media_type="audio/mpeg",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes"
                }
            )
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Download failed: {str(e)}")


@app.post("/download/direct")
async def download_direct(data: DownloadIn):
    """
    Direct download using multiple fallback methods.
    Optimized for both localhost and Render deployment.
    
    Parameters:
    - videoId: YouTube video ID (required)
    - quality: 1=low (96kbps), 2=medium (128kbps), 3=high (320kbps)
    
    Output format is always MP3.
    """
    video_id = data.videoId
    format_ext = "mp3"  # Always MP3 format
    quality = data.quality if hasattr(data, 'quality') else 2
    
    # Validate quality
    if quality not in [1, 2, 3]:
        raise HTTPException(400, "Quality must be 1 (low), 2 (medium), or 3 (high)")
    
    # Check cache first
    cached_file = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
    if os.path.exists(cached_file):
        file_size = os.path.getsize(cached_file)
        
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
            headers={
                "Content-Disposition": f'attachment; filename="{video_id}.{format_ext}"',
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes"
            }
        )

    # Try Method 0: RapidAPI (preferred, handles bot-detection)
    rapidapi_key = os.getenv("RAPIDAPI_KEY")
    rapidapi_host = "youtube-media-downloader.p.rapidapi.com"
    if rapidapi_key:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(
                    f"https://{rapidapi_host}/v2/video/streams",
                    params={"videoId": video_id},
                    headers={
                        "x-rapidapi-key": rapidapi_key,
                        "x-rapidapi-host": rapidapi_host,
                    }
                )

                if resp.status_code == 200:
                    data = resp.json()
                    streams = data.get("streams") or data.get("formats") or []

                    # Pick best audio-only stream
                    audio_streams = []
                    for s in streams:
                        mime = (s.get("mimeType") or s.get("type") or "").lower()
                        if "audio" in mime:
                            audio_streams.append(s)

                    if audio_streams:
                        # sort by bitrate descending if available
                        audio_streams.sort(key=lambda x: x.get("bitrate") or x.get("kbps") or 0, reverse=True)
                        best = audio_streams[0]
                        audio_url = best.get("url") or best.get("downloadUrl")

                        if audio_url:
                            tmpdir = tempfile.mkdtemp(prefix="dl_")
                            temp_file = os.path.join(tmpdir, f"{video_id}.{format_ext}")

                            async with client.stream("GET", audio_url) as dresp:
                                dresp.raise_for_status()
                                with open(temp_file, "wb") as f:
                                    async for chunk in dresp.aiter_bytes(65536):
                                        f.write(chunk)

                            # Cache file
                            try:
                                import shutil
                                shutil.copy2(temp_file, cached_file)
                            except Exception:
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
                                        import shutil
                                        shutil.rmtree(tmpdir)
                                    except Exception:
                                        pass

                            file_size = os.path.getsize(temp_file)
                            
                            return StreamingResponse(
                                file_stream(),
                                media_type="audio/mpeg",
                                headers={
                                    "Content-Disposition": f'attachment; filename="{video_id}.{format_ext}"',
                                    "Content-Length": str(file_size),
                                    "Accept-Ranges": "bytes"
                                }
                            )
        except Exception as exc:
            print(f"RapidAPI download failed: {exc}")
    
    tmpdir = tempfile.mkdtemp(prefix="dl_")
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    # Try Method 1: you-get (different extraction method, may bypass blocks)
    try:
        def youget_download():
            import subprocess
            output_file = os.path.join(tmpdir, f"{video_id}")
            
            cmd = [
                "you-get",
                "-o", tmpdir,
                "-O", video_id,
                "--format=dash-flv720",
                url
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            # Find downloaded file
            files = [f for f in os.listdir(tmpdir) if not f.startswith('.')]
            if files:
                return os.path.join(tmpdir, files[0])
            return None
        
        loop = asyncio.get_event_loop()
        downloaded_file = await loop.run_in_executor(None, youget_download)
        
        if downloaded_file and os.path.exists(downloaded_file):
            # Convert to desired format using ffmpeg
            output_file = os.path.join(tmpdir, f"output.{format_ext}")
            
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", downloaded_file, "-q:a", "0", "-map", "a", 
                output_file, "-y",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            
            if os.path.exists(output_file):
                # Cache it
                try:
                    import shutil
                    shutil.copy2(output_file, cached_file)
                except:
                    pass
                
                filename = f"{video_id}.{format_ext}"
                
                def file_stream():
                    try:
                        with open(output_file, "rb") as f:
                            while True:
                                chunk = f.read(65536)
                                if not chunk:
                                    break
                                yield chunk
                    finally:
                        try:
                            import shutil
                            shutil.rmtree(tmpdir)
                        except:
                            pass
                
                file_size = os.path.getsize(output_file)
                
                return StreamingResponse(
                    file_stream(),
                    media_type="audio/mpeg",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Content-Length": str(file_size),
                        "Accept-Ranges": "bytes"
                    }
                )
    except Exception as e:
        print(f"you-get failed: {e}")
    
    # Try Method 2: pytube (often works better than yt-dlp on cloud)
    try:
        def pytube_download():
            from pytube import YouTube
            yt = YouTube(url)
            stream = yt.streams.filter(only_audio=True, file_extension='mp4').order_by('abr').desc().first()
            
            if stream:
                downloaded_file = stream.download(output_path=tmpdir, filename="audio.mp4")
                return downloaded_file, yt.title
            return None, None
        
        loop = asyncio.get_event_loop()
        downloaded_file, title = await loop.run_in_executor(None, pytube_download)
        
        if downloaded_file and os.path.exists(downloaded_file):
            # Convert to desired format using ffmpeg
            output_file = os.path.join(tmpdir, f"output.{format_ext}")
            
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", downloaded_file, "-q:a", "0", "-map", "a", 
                output_file, "-y",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            
            if os.path.exists(output_file):
                # Cache it
                try:
                    import shutil
                    shutil.copy2(output_file, cached_file)
                except:
                    pass
                
                filename = f"{title}.{format_ext}" if title else f"{video_id}.{format_ext}"
                filename = "".join(c for c in filename if ord(c) < 128 or c in ' -_.')
                
                def file_stream():
                    try:
                        with open(output_file, "rb") as f:
                            while True:
                                chunk = f.read(65536)
                                if not chunk:
                                    break
                                yield chunk
                    finally:
                        try:
                            import shutil
                            shutil.rmtree(tmpdir)
                        except:
                            pass
                
                file_size = os.path.getsize(output_file)
                
                return StreamingResponse(
                    file_stream(),
                    media_type="audio/mpeg",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Content-Length": str(file_size),
                        "Accept-Ranges": "bytes"
                    }
                )
    except Exception as e:
        print(f"pytube failed: {e}")
    
    # Try Method 3: yt-dlp with latest best practices
    try:
        from yt_dlp import YoutubeDL
        
        # Get ffmpeg location from bin/ directory
        bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bin')
        
        # Get optimized yt-dlp options
        ydl_opts = get_yt_dlp_options(tmpdir, bin_dir, format_ext, quality)
        
        def download_sync():
            with YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=True)
                    return info.get('title', video_id)
                except Exception as e:
                    raise HTTPException(500, f"yt-dlp extraction failed: {str(e)}")
        
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
            except Exception:
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
                        import shutil
                        shutil.rmtree(tmpdir)
                    except Exception:
                        pass
            
            file_size = os.path.getsize(file_path)
            
            return StreamingResponse(
                file_stream(),
                media_type="audio/mpeg",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes"
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"yt-dlp failed: {e}")
    
    # Cleanup temp dir if all methods failed
    try:
        import shutil
        shutil.rmtree(tmpdir)
    except:
        pass
    
    raise HTTPException(503, "Download failed. This service requires pre-cached files on Render. Please contact admin to cache this song.")


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
    file_size = os.path.getsize(file_path)
    
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
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes"
        }
    )
    

def extract_spotify_playlist_id(link: str) -> str:
    """Extract playlist ID from Spotify URL or URI."""
    if not link:
        return ""
    
    # Handle different URL formats
    if 'spotify.com' in link:
        # Extract from web URL like: https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
        parsed = urlparse(link)
        playlist_id = parsed.path.split('/')[-1]
        # Remove query parameters if present
        playlist_id = playlist_id.split('?')[0]
        return playlist_id
    elif 'spotify:playlist:' in link:
        # Extract from URI like: spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
        return link.split(':')[-1]
    else:
        # Assume it's already just the ID
        return link


async def get_spotify_access_token(client_id: str, client_secret: str) -> str:
    """Get Spotify access token using Client Credentials flow."""
    import httpx
    import base64
    
    # Encode credentials
    credentials = f"{client_id}:{client_secret}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()
    
    # Request token
    headers = {
        'Authorization': f'Basic {encoded_credentials}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    data = {
        'grant_type': 'client_credentials'
    }
    
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            'https://accounts.spotify.com/api/token',
            headers=headers,
            data=data
        )

        if response.status_code == 200:
            token_data = response.json()
            return token_data['access_token']

        raise HTTPException(500, f"Error getting Spotify token: {response.status_code}")


async def get_cached_spotify_access_token(client_id: str, client_secret: str) -> str:
    """Return cached Spotify token if valid; otherwise fetch and cache a new one."""
    now = datetime.utcnow().timestamp()
    if spotify_token_cache["token"] and now < spotify_token_cache["expires_at"] - 60:
        return spotify_token_cache["token"]

    token = await get_spotify_access_token(client_id, client_secret)
    # Token TTL is 3600s; refresh slightly early
    spotify_token_cache["token"] = token
    spotify_token_cache["expires_at"] = now + 3500
    return token


async def fetch_spotify_playlist(playlist_id: str, access_token: str) -> dict:
    """Fetch all tracks from a Spotify playlist."""
    import httpx
    
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    
    tracks = []
    url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks'
    
    async with httpx.AsyncClient() as client:
        while url:
            # Parameters to get specific fields and handle pagination
            params = {
                'limit': 50,  # Max items per request
                'fields': 'items(added_at,track(id,name,artists(name),album(name,release_date,images),duration_ms,popularity,external_urls)),next'
            }
            
            response = await client.get(url, headers=headers, params=params)
            
            if response.status_code == 200:
                data = response.json()
                
                for item in data['items']:
                    track = item.get('track')
                    if track:  # Some tracks might be None (removed/unavailable)
                        artists = track.get('artists', [])
                        album = track.get('album', {})
                        
                        tracks.append({
                            'name': track.get('name', 'N/A'),
                            'artists': [{'name': artist['name']} for artist in artists],
                            'album': {
                                'name': album.get('name', 'N/A'),
                                'images': album.get('images', [])
                            },
                            'duration_ms': track.get('duration_ms', 0),
                            'popularity': track.get('popularity', 0),
                            'spotify_url': track.get('external_urls', {}).get('spotify', 'N/A'),
                            'track_id': track.get('id', 'N/A')
                        })
                
                # Check if there are more pages
                url = data.get('next')
            else:
                raise HTTPException(500, f"Error fetching tracks: {response.status_code}")
    
    return tracks


async def fetch_spotify_playlist_info(playlist_id: str, access_token: str):
    """Fetch minimal playlist metadata (owner/name)."""
    import httpx

    url = f"https://api.spotify.com/v1/playlists/{playlist_id}"
    params = {"fields": "owner(display_name),name"}
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"Failed to fetch playlist info: {resp.text}")
        return resp.json()


class SpotifyPlaylistRequest(BaseModel):
    link: str


@app.post("/spotifyPlaylist")
async def spotify_playlist(request: SpotifyPlaylistRequest):
    """
    Fetch Spotify playlist using `spotify-playlist-extractor` for URL parsing,
    Spotify Web API for data, and enrich with YTMusic videoIds.
    """
    import spotify_extractor.cli as spe

    link = request.link

    # Use the extractor's parsing and token logic (no client credentials required here)
    def extract():
        playlist_id = spe.extract_playlist_id_from_url(link)
        if not playlist_id:
            raise HTTPException(400, "Invalid Spotify playlist link")

        token = spe.get_access_token()
        if not token:
            raise HTTPException(500, "Failed to obtain Spotify access token")

        info = spe.get_playlist_info(playlist_id, token)
        tracks = spe.get_all_tracks_from_playlist(playlist_id, token)
        return playlist_id, info, tracks

    try:
        playlist_id, info, raw_tracks = await asyncio.to_thread(extract)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Spotify fetch error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Failed to fetch playlist: {e}")

    playlist_author = (info or {}).get("owner", {}).get("display_name") if info else "Spotify"
    playlist_name = (info or {}).get("name") if info else "Unknown Playlist"

    # Debug: log first track to see available fields
    if raw_tracks:
        print(f"DEBUG: First track keys: {list(raw_tracks[0].keys())}")
        print(f"DEBUG: First track: {raw_tracks[0]}")

    # Parallelize YTMusic lookups with bounded concurrency
    search_sem = asyncio.Semaphore(8)

    async def enrich(track: dict):
        title = track.get("name")
        if not title:
            return None

        artists_str = track.get("artist", "")
        artists = [a.strip() for a in artists_str.split(",") if a.strip()]
        thumbnail = None
        duration = None

        query = f"{title} {artists[0] if artists else ''}".strip()
        video_id = None

        async with search_sem:
            try:
                yt_results = await asyncio.to_thread(
                    ytmusic.search,
                    query,
                    filter="songs",
                    limit=1
                )
                if yt_results:
                    top = yt_results[0]
                    video_id = top.get("videoId")
                    # Get duration from YTMusic (in seconds)
                    duration = top.get("duration")
                    thumbs = top.get("thumbnails") or []
                    if thumbs:
                        thumbnail = thumbs[-1].get("url") or thumbnail
            except Exception as e:
                print(f"YTMusic search failed for '{query}': {e}")

        return {
            "title": title,
            "authors": artists,
            "videoId": video_id,
            "thumbnail": thumbnail,
            "duration": duration
        }

    enriched = await asyncio.gather(*(enrich(t) for t in (raw_tracks or [])))
    tracks = [t for t in enriched if t]

    return {
        "playlistAuthor": playlist_author or "Spotify",
        "playlistName": playlist_name,
        "trackCount": len(tracks),
        "tracks": tracks
    }


def extract_youtube_playlist_id(link: str) -> str:
    """Extract playlist ID from YouTube/YouTube Music URL."""
    if not link:
        return ""
    
    # Handle various YouTube playlist URL formats
    # https://www.youtube.com/playlist?list=PLxxxxxx
    # https://music.youtube.com/playlist?list=PLxxxxxx
    # https://youtu.be/xxxxxx?list=PLxxxxxx
    
    parsed = urlparse(link)
    query_params = parse_qs(parsed.query)
    
    if "list" in query_params:
        return query_params["list"][0]
    
    return ""


class YouTubePlaylistRequest(BaseModel):
    link: str


@app.post("/youtubePlaylist")
async def youtube_playlist(request: YouTubePlaylistRequest):
    """
    Fetch YouTube/YouTube Music playlist and return track details with videoIds.
    
    Request body:
    {
        "link": "youtube_playlist_url"
    }
    
    Response:
    {
        "playlistAuthor": "Channel Name",
        "playlistName": "Playlist Title",
        "trackCount": 10,
        "tracks": [
            {
                "title": "Song Name",
                "authors": ["Artist Name"],
                "videoId": "xxxxxxxxx",
                "thumbnail": "https://..."
            }
        ]
    }
    """
    link = request.link
    playlist_id = extract_youtube_playlist_id(link)
    
    if not playlist_id:
        raise HTTPException(400, "Invalid YouTube playlist link")
    
    print(f"Fetching YouTube playlist: {playlist_id}")
    
    try:
        # Fetch playlist data using ytmusicapi
        playlist_data = await asyncio.to_thread(
            ytmusic.get_playlist,
            playlist_id,
            limit=None  # Get all tracks
        )
        
        if not playlist_data:
            raise HTTPException(404, "Playlist not found")
        
        playlist_name = playlist_data.get("title", "Unknown Playlist")
        playlist_author = playlist_data.get("author", {}).get("name", "Unknown") if isinstance(playlist_data.get("author"), dict) else playlist_data.get("author", "Unknown")
        playlist_tracks = playlist_data.get("tracks", [])
        
        print(f"Playlist: {playlist_name} by {playlist_author}")
        print(f"Total tracks: {len(playlist_tracks)}")
        
        tracks = []
        for track in playlist_tracks:
            if not track:
                continue
            
            title = track.get("title", "Unknown")
            video_id = track.get("videoId")
            
            # Extract artists
            artists = track.get("artists", [])
            if isinstance(artists, list):
                authors = [artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")]
            else:
                authors = []
            
            # Get thumbnail
            thumbnails = track.get("thumbnails", [])
            thumbnail = thumbnails[-1].get("url") if thumbnails else None
            
            # Get duration (in seconds)
            duration = track.get("duration")
            
            tracks.append({
                "title": title,
                "authors": authors,
                "videoId": video_id,
                "thumbnail": thumbnail,
                "duration": duration
            })
        
        return {
            "playlistAuthor": playlist_author,
            "playlistName": playlist_name,
            "trackCount": len(tracks),
            "tracks": tracks
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching playlist: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Failed to fetch playlist: {str(e)}")
    
@app.get("/search")
def search(q: str, type: str = "all"):
    """
    Search for songs, artists, or albums.
    
    Parameters:
    - q: Search query (required)
    - type: Filter type - "all", "songs", "artists", or "albums" (default: "all")
    
    Example:
    /search?q=Na%20re%20na&type=songs
    """
    try:
        # Validate type parameter
        valid_types = ["all", "songs", "artists", "albums"]
        if type not in valid_types:
            raise HTTPException(400, f"Invalid type. Must be one of: {', '.join(valid_types)}")
        
        # Map type to ytmusicapi filter parameter
        filter_map = {
            "all": None,  # No filter for "all"
            "songs": "songs",
            "artists": "artists",
            "albums": "albums"
        }
        
        filter_param = filter_map[type]
        
        # Perform search
        if filter_param:
            results = ytmusic.search(q, filter=filter_param, limit=20)
        else:
            # For "all", get results from multiple filter types
            songs = ytmusic.search(q, filter="songs", limit=10)
            artists = ytmusic.search(q, filter="artists", limit=10)
            albums = ytmusic.search(q, filter="albums", limit=10)
            results = {
                "songs": songs,
                "artists": artists,
                "albums": albums
            }

        if not results:
            raise HTTPException(404, "No results found")

        # Format results based on type
        if type == "all":
            return {
                "songs": format_search_results(results.get("songs", []), "song"),
                "artists": format_search_results(results.get("artists", []), "artist"),
                "albums": format_search_results(results.get("albums", []), "album")
            }
        elif type == "songs":
            return format_search_results(results, "song")
        elif type == "artists":
            return format_search_results(results, "artist")
        elif type == "albums":
            return format_search_results(results, "album")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Search error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Search failed: {str(e)}")


def format_search_results(items, item_type):
    """Format search results based on item type"""
    formatted_results = []
    
    # Ensure items is a list
    if not isinstance(items, list):
        return formatted_results
    
    for item in items:
        if not isinstance(item, dict):
            continue
            
        if item_type == "song":
            # Format artists safely
            artists = item.get("artists", [])
            artist_names = []
            if isinstance(artists, list):
                artist_names = [artist.get("name") for artist in artists if isinstance(artist, dict) and artist.get("name")]
            
            formatted_results.append({
                "type": "song",
                "title": item.get("title"),
                "artists": artist_names,
                "duration": item.get("duration"),
                "thumbnail": item.get("thumbnails", [{}])[-1].get("url") if item.get("thumbnails") else None,
                "videoId": item.get("videoId")
            })
        elif item_type == "artist":
            # Try to get artist name from various fields
            artist_name = item.get("title") or item.get("name") or item.get("subtitle")
            
            # If name is still not available, try to fetch from browseId
            if not artist_name and item.get("browseId"):
                try:
                    artist_info = ytmusic.get_artist(item.get("browseId"))
                    artist_name = artist_info.get("name")
                except Exception as e:
                    print(f"Failed to fetch artist info for {item.get('browseId')}: {e}")
            
            formatted_results.append({
                "type": "artist",
                "name": artist_name,
                "thumbnail": item.get("thumbnails", [{}])[-1].get("url") if item.get("thumbnails") else None,
                "browseId": item.get("browseId")
            })
        elif item_type == "album":
            # Format artists safely
            artists = item.get("artists", [])
            artist_names = []
            if isinstance(artists, list):
                artist_names = [artist.get("name") for artist in artists if isinstance(artist, dict) and artist.get("name")]
            
            formatted_results.append({
                "type": "album",
                "title": item.get("title"),
                "artists": artist_names,
                "year": item.get("year"),
                "thumbnail": item.get("thumbnails", [{}])[-1].get("url") if item.get("thumbnails") else None,
                "browseId": item.get("browseId")
            })
    
    return formatted_results


@app.get("/artist/{browseId}")
def get_artist_details(browseId: str):
    """
    Get detailed information about an artist by browseId.
    
    Example:
    /artist/UCPC0L1d253x-KuMNwa05TpA
    
    Returns: artist name, description, thumbnail, top songs, albums, singles, etc.
    Note: browseId should be an artist catalog ID, not a channel ID (UC...).
    """
    try:
        artist_info = ytmusic.get_artist(browseId)
        
        if not artist_info:
            raise HTTPException(404, "Artist not found")
        
        # Format the response
        response = {
            "name": artist_info.get("name"),
            "description": artist_info.get("description"),
            "thumbnail": artist_info.get("thumbnails", [{}])[-1].get("url") if artist_info.get("thumbnails") else None,
            "browseId": browseId
        }
        
        # Add top songs if available
        if artist_info.get("songs"):
            response["topSongs"] = format_search_results(artist_info.get("songs", []), "song")
        
        # Add albums if available
        if artist_info.get("albums"):
            response["albums"] = format_search_results(artist_info.get("albums", []), "album")
        
        # Add singles if available
        if artist_info.get("singles"):
            response["singles"] = format_search_results(artist_info.get("singles", []), "album")
        
        return response
        
    except Exception as e:
        print(f"Error fetching artist details: {e}")
        raise HTTPException(500, f"Failed to fetch artist details: {str(e)}")


@app.get("/album/{browseId}")
def get_album_details(browseId: str):
    """
    Get detailed information about an album by browseId.
    
    Example:
    /album/MPREb_XUWTmZUXJVt
    
    Returns: title, artists, tracks, year, release date, thumbnail, etc.
    """
    try:
        album_info = ytmusic.get_album(browseId)
        
        if not album_info:
            raise HTTPException(404, "Album not found")
        
        # Format the response
        response = {
            "title": album_info.get("title"),
            "artists": [{"name": artist.get("name"), "browseId": artist.get("id")} 
                       for artist in album_info.get("artists", [])],
            "year": album_info.get("year"),
            "releaseDate": album_info.get("releaseDate"),
            "thumbnail": album_info.get("thumbnails", [{}])[-1].get("url") if album_info.get("thumbnails") else None,
            "browseId": browseId
        }
        
        # Add tracks if available
        if album_info.get("tracks"):
            response["tracks"] = format_search_results(album_info.get("tracks", []), "song")
        
        # Add description/subtitle if available
        if album_info.get("description"):
            response["description"] = album_info.get("description")
        
        # Add duration if available
        if album_info.get("duration"):
            response["duration"] = album_info.get("duration")
        
        return response
        
    except Exception as e:
        print(f"Error fetching album details: {e}")
        raise HTTPException(500, f"Failed to fetch album details: {str(e)}")


@app.get("/album")
def get_album_songs(browseId: str):
    """
    Get all songs from an album by browseId via query parameter.
    
    Example:
    GET /album?browseId=MPREb_XUWTmZUXJVt
    
    Returns: List of all songs in the album with their details
    """
    if not browseId:
        raise HTTPException(400, "browseId query parameter is required")
    
    try:
        album_info = ytmusic.get_album(browseId)
        
        if not album_info:
            raise HTTPException(404, "Album not found")
        
        # Return all tracks from the album
        tracks = album_info.get("tracks", [])
        
        if not tracks:
            return {
                "browseId": browseId,
                "title": album_info.get("title"),
                "songs": []
            }
        
        # Format the tracks
        formatted_tracks = format_search_results(tracks, "song")
        
        # Add YouTube thumbnail for songs that don't have one
        for song in formatted_tracks:
            if not song.get("thumbnail") and song.get("videoId"):
                song["thumbnail"] = f"https://i.ytimg.com/vi/{song['videoId']}/mqdefault.jpg"
        
        return {
            "browseId": browseId,
            "title": album_info.get("title"),
            "artists": [{"name": artist.get("name"), "browseId": artist.get("id")} 
                       for artist in album_info.get("artists", [])],
            "songs": formatted_tracks,
            "totalSongs": len(formatted_tracks)
        }
        
    except Exception as e:
        print(f"Error fetching album songs: {e}")
        raise HTTPException(500, f"Failed to fetch album songs: {str(e)}")


class LyricsRequest(BaseModel):
    videoId: Optional[str] = None
    artistName: Optional[str] = None
    trackName: Optional[str] = None


async def fetch_lrclib_lyrics(artist_name: str, track_name: str) -> Optional[dict]:
    """Fetch synced lyrics from LRCLib."""
    params = {
        "artist_name": artist_name,
        "track_name": track_name
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("https://lrclib.net/api/get", params=params)
    except httpx.RequestError as exc:
        print(f"LRCLib request error: {exc}")
        return None

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        print(f"LRCLib unexpected status: {response.status_code}")
        return None

    try:
        data = response.json()
    except ValueError:
        print("LRCLib returned invalid JSON")
        return None

    if not data.get("syncedLyrics"):
        return None

    return data


@app.post("/lyrics")
async def get_lyrics(request: LyricsRequest):
    """
    Fetch lyrics using LRCLib first (synced), fallback to YouTube Music.
    """
    video_id = request.videoId.strip() if request.videoId else None
    artist_name = request.artistName.strip() if request.artistName else None
    track_name = request.trackName.strip() if request.trackName else None

    if not ((artist_name and track_name) or video_id):
        raise HTTPException(400, "artistName and trackName or videoId is required")
    
    try:
        # Try LRCLib first if artist and track names provided
        if artist_name and track_name:
            lrclib_data = await fetch_lrclib_lyrics(artist_name, track_name)
            if lrclib_data:
                return {
                    "syncedLyrics": lrclib_data.get("syncedLyrics"),
                    "source": "lrclib",
                    "returner": "lrclib"
                }

        # LRCLib failed or not provided, try YouTube Music
        if not video_id and artist_name and track_name:
            search_query = f"{track_name} {artist_name}"
            search_results = await asyncio.to_thread(
                ytmusic.search,
                search_query,
                filter="songs",
                limit=1
            )

            if search_results:
                video_id = search_results[0].get("videoId")

        if not video_id:
            raise HTTPException(404, "Lyrics not found")

        # Get watch playlist which contains lyrics info
        watch_data = await asyncio.to_thread(
            ytmusic.get_watch_playlist,
            video_id
        )
        
        if not watch_data or "lyrics" not in watch_data:
            raise HTTPException(404, "Lyrics not available for this song")
        
        lyrics_browse_id = watch_data["lyrics"]
        
        # Fetch actual lyrics
        try:
            lyrics_data = await asyncio.to_thread(
                ytmusic.get_lyrics,
                lyrics_browse_id
            )
        except Exception as e:
            # If get_lyrics fails, lyrics likely don't exist for this song
            print(f"get_lyrics failed: {e}")
            raise HTTPException(404, "Lyrics not available for this song")
        
        if not lyrics_data or "lyrics" not in lyrics_data:
            raise HTTPException(404, "Lyrics not found")
            # If get_lyrics fails, lyrics likely don't exist for this song
            print(f"get_lyrics failed: {e}")
            raise HTTPException(404, "Lyrics not available for this song")
        
        if not lyrics_data or "lyrics" not in lyrics_data:
            raise HTTPException(404, "Lyrics not found")
        
        return {
            "lyrics": lyrics_data["lyrics"],
            "source": lyrics_data.get("source", "YouTube Music"),
            "returner": "ytmusic"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching lyrics: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Failed to fetch lyrics: {str(e)}")


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


@app.post("/download/playlist")
async def download_playlist(data: PlaylistDownloadIn):
    """
    Download an entire playlist as a ZIP file containing all MP3 files.
    
    Request body:
    {
        "videoIds": ["video_id_1", "video_id_2", "video_id_3"],
        "quality": 2
    }
    
    quality: 1=low (96kbps), 2=medium (128kbps, default), 3=high (320kbps)
    """
    import zipfile
    import shutil
    from io import BytesIO
    
    video_ids = data.videoIds
    quality = data.quality if hasattr(data, 'quality') else 2
    format_ext = "mp3"
    
    # Validate quality
    if quality not in [1, 2, 3]:
        raise HTTPException(400, "Quality must be 1 (low), 2 (medium), or 3 (high)")
    
    # Validate video IDs
    if not video_ids or not isinstance(video_ids, list):
        raise HTTPException(400, "videoIds must be a non-empty list")
    
    if len(video_ids) > 100:
        raise HTTPException(400, "Maximum 100 videos per playlist allowed")
    
    # Create temporary directory for playlist downloads
    playlist_tmpdir = tempfile.mkdtemp(prefix="playlist_")
    zip_path = os.path.join(playlist_tmpdir, "playlist.zip")
    
    try:
        downloaded_count = 0
        failed_videos = []
        
        # Download each video
        for idx, video_id in enumerate(video_ids, 1):
            try:
                # Check cache first
                cached_file = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
                
                if os.path.exists(cached_file):
                    # For cached files, we need to get the original title
                    # Try to extract title from YoutubeDL info
                    tmpdir_info = tempfile.mkdtemp(prefix="info_")
                    url = f"https://www.youtube.com/watch?v={video_id}"
                    title = video_id
                    
                    try:
                        from yt_dlp import YoutubeDL
                        bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bin')
                        ydl_opts = get_yt_dlp_options(tmpdir_info, bin_dir, format_ext, quality)
                        ydl_opts['skip_download'] = True  # Only get info, don't download
                        
                        def get_title():
                            with YoutubeDL(ydl_opts) as ydl:
                                try:
                                    info = ydl.extract_info(url, download=False)
                                    return info.get('title', video_id)
                                except:
                                    return video_id
                        
                        loop = asyncio.get_event_loop()
                        title = await loop.run_in_executor(None, get_title)
                    except:
                        title = video_id
                    finally:
                        try:
                            shutil.rmtree(tmpdir_info)
                        except:
                            pass
                    
                    # Sanitize filename
                    safe_title = "".join(c for c in title if ord(c) < 128 or c in ' -_.')
                    dest_path = os.path.join(playlist_tmpdir, f"{idx:03d}_{safe_title}.{format_ext}")
                    shutil.copy2(cached_file, dest_path)
                    downloaded_count += 1
                else:
                    # Download fresh
                    tmpdir = tempfile.mkdtemp(prefix="dl_")
                    url = f"https://www.youtube.com/watch?v={video_id}"
                    
                    try:
                        from yt_dlp import YoutubeDL
                        
                        # Get ffmpeg location from bin/ directory
                        bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bin')
                        
                        # Get optimized yt-dlp options
                        ydl_opts = get_yt_dlp_options(tmpdir, bin_dir, format_ext, quality)
                        
                        def download_sync():
                            with YoutubeDL(ydl_opts) as ydl:
                                try:
                                    info = ydl.extract_info(url, download=True)
                                    return info.get('title', video_id)
                                except Exception as e:
                                    error_msg = str(e)
                                    if '403' in error_msg or '429' in error_msg:
                                        return None
                                    raise Exception(f"yt-dlp extraction failed: {error_msg}")
                        
                        loop = asyncio.get_event_loop()
                        title = await loop.run_in_executor(None, download_sync)
                        
                        if title is None:
                            failed_videos.append(video_id)
                            continue
                        
                        # Find downloaded file
                        files = [f for f in os.listdir(tmpdir) if f.endswith(f".{format_ext}")]
                        
                        if files:
                            file_path = os.path.join(tmpdir, files[0])
                            # Sanitize filename for safe filesystem usage
                            safe_title = "".join(c for c in title if ord(c) < 128 or c in ' -_.')
                            dest_path = os.path.join(playlist_tmpdir, f"{idx:03d}_{safe_title}.{format_ext}")
                            shutil.copy2(file_path, dest_path)
                            
                            # Also cache for future requests
                            try:
                                cached_path = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
                                shutil.copy2(file_path, cached_path)
                            except Exception:
                                pass
                            
                            downloaded_count += 1
                    finally:
                        try:
                            shutil.rmtree(tmpdir)
                        except Exception:
                            pass
                            
            except Exception as e:
                print(f"Failed to download video {video_id}: {e}")
                failed_videos.append(video_id)
        
        if downloaded_count == 0:
            raise HTTPException(500, "Failed to download any videos from the playlist")
        
        # Create ZIP file with all downloaded songs
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            files = sorted([f for f in os.listdir(playlist_tmpdir) if f.endswith(f".{format_ext}")])
            for file in files:
                file_path = os.path.join(playlist_tmpdir, file)
                # Remove the numeric prefix when adding to ZIP (keep original title)
                if '_' in file:
                    arcname = file.split('_', 1)[1]  # Remove "001_" prefix, keep "Song Title.mp3"
                else:
                    arcname = file
                zipf.write(file_path, arcname)
        
        # Read ZIP file and stream it
        zip_size = os.path.getsize(zip_path)
        
        def zip_stream():
            try:
                with open(zip_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    shutil.rmtree(playlist_tmpdir)
                except Exception:
                    pass
        
        return StreamingResponse(
            zip_stream(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="playlist.zip"',
                "Content-Length": str(zip_size),
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        try:
            shutil.rmtree(playlist_tmpdir)
        except Exception:
            pass
        raise HTTPException(500, f"Playlist download failed: {str(e)}")

#icon, song name, singers