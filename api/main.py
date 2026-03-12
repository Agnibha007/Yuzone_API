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

spotify_token_cache = {
    "token": None,
    "expires_at": 0.0
}

download_semaphore = asyncio.Semaphore(5)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "downloads")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MANIFEST = os.path.join(CACHE_DIR, "manifest.json")


class DownloadIn(BaseModel):
    videoId: str
    quality: int = 2


class PlaylistDownloadIn(BaseModel):
    videoIds: list
    quality: int = 2


def get_quality_settings(quality: int) -> dict:
    quality_map = {
        1: {"bitrate": "96k", "vbr": "9"},
        2: {"bitrate": "128k", "vbr": "6"},
        3: {"bitrate": "320k", "vbr": "0"}
    }
    if quality not in quality_map:
        quality = 2
    return quality_map[quality]


def get_yt_dlp_options(tmpdir: str, bin_dir: str, format_ext: str, quality: int) -> dict:
    """
    yt-dlp options tuned to avoid YouTube bot detection on Render.
    Uses iOS client first (least fingerprinted), with android + web as fallback.
    Cookies file is loaded if present — place cookies.txt in project root.
    """
    quality_settings = get_quality_settings(quality)
    cookies_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cookies.txt")

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[height<=480]/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": format_ext,
            "preferredquality": quality_settings["bitrate"],
            "nopostoverwrites": False,
        }],
        "outtmpl": os.path.join(tmpdir, "%(id)s"),
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 30,
        "ffmpeg_location": bin_dir,
        "keepvideo": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        "concurrent_fragment_downloads": 4,
        "fragment_retries": 10,
        "file_access_retries": 15,
        "retries": 10,
        "skip_unavailable_fragments": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "ios", "android", "web"],
                "player_skip": ["webpage", "configs"],
            }
        },
    }

    if os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file

    return opts


@app.get("/")
def root():
    return RedirectResponse(url="/top")


@app.post("/webhook/deploy")
async def github_webhook(request: Request):
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

    if payload.get("ref") in ["refs/heads/main", "refs/heads/master"]:
        try:
            repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            result = subprocess.run(
                ["git", "pull", "origin", "master"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
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
        format_ext = "mp3"
        quality = data.quality if hasattr(data, "quality") else 2

        if quality not in [1, 2, 3]:
            raise HTTPException(400, "Quality must be 1 (low), 2 (medium), or 3 (high)")

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
                    "Accept-Ranges": "bytes",
                }
            )

        tmpdir = tempfile.mkdtemp(prefix="dl_")
        url = f"https://www.youtube.com/watch?v={video_id}"

        try:
            from yt_dlp import YoutubeDL

            bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin")
            ydl_opts = get_yt_dlp_options(tmpdir, bin_dir, format_ext, quality)

            def download_sync():
                with YoutubeDL(ydl_opts) as ydl:
                    try:
                        info = ydl.extract_info(url, download=True)
                        return info.get("title", video_id)
                    except Exception as e:
                        error_msg = str(e)
                        if "Sign in" in error_msg or "bot" in error_msg.lower():
                            raise HTTPException(
                                403,
                                "YouTube bot detection triggered. Add a valid cookies.txt to the project root."
                            )
                        elif "403" in error_msg:
                            raise HTTPException(403, "YouTube returned 403. Cookies may be expired.")
                        elif "429" in error_msg:
                            raise HTTPException(429, "Rate limited by YouTube. Please wait before retrying.")
                        else:
                            raise HTTPException(500, f"yt-dlp extraction failed: {error_msg}")

            loop = asyncio.get_event_loop()
            title = await loop.run_in_executor(None, download_sync)

            files = [f for f in os.listdir(tmpdir) if f.endswith(f".{format_ext}")]

            if not files:
                raise HTTPException(500, "Audio file not created after postprocessing")

            file_path = os.path.join(tmpdir, files[0])
            filename = f"{title}.{format_ext}" if title else files[0]
            filename = "".join(c for c in filename if ord(c) < 128 or c in " -_.")

            try:
                import shutil
                shutil.copy2(file_path, os.path.join(CACHE_DIR, f"{video_id}.{format_ext}"))
            except Exception:
                pass

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
                    except Exception:
                        pass

            return StreamingResponse(
                file_stream(),
                media_type="audio/mpeg",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes",
                }
            )

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Download failed: {str(e)}")


@app.post("/download/direct")
async def download_direct(data: DownloadIn):
    """
    Direct download with RapidAPI → pytube → yt-dlp fallback chain.

    Parameters:
    - videoId: YouTube video ID (required)
    - quality: 1=low (96kbps), 2=medium (128kbps), 3=high (320kbps)
    """
    video_id = data.videoId
    format_ext = "mp3"
    quality = data.quality if hasattr(data, "quality") else 2

    if quality not in [1, 2, 3]:
        raise HTTPException(400, "Quality must be 1 (low), 2 (medium), or 3 (high)")

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
                "Accept-Ranges": "bytes",
            }
        )

    # --- Method 0: RapidAPI ---
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
                    rdata = resp.json()
                    streams = rdata.get("streams") or rdata.get("formats") or []
                    audio_streams = [
                        s for s in streams
                        if "audio" in (s.get("mimeType") or s.get("type") or "").lower()
                    ]
                    if audio_streams:
                        audio_streams.sort(
                            key=lambda x: x.get("bitrate") or x.get("kbps") or 0,
                            reverse=True
                        )
                        audio_url = audio_streams[0].get("url") or audio_streams[0].get("downloadUrl")
                        if audio_url:
                            tmpdir = tempfile.mkdtemp(prefix="dl_")
                            temp_file = os.path.join(tmpdir, f"{video_id}.{format_ext}")
                            async with client.stream("GET", audio_url) as dresp:
                                dresp.raise_for_status()
                                with open(temp_file, "wb") as f:
                                    async for chunk in dresp.aiter_bytes(65536):
                                        f.write(chunk)
                            try:
                                import shutil
                                shutil.copy2(temp_file, cached_file)
                            except Exception:
                                pass
                            file_size = os.path.getsize(temp_file)

                            def file_stream_rapid():
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

                            return StreamingResponse(
                                file_stream_rapid(),
                                media_type="audio/mpeg",
                                headers={
                                    "Content-Disposition": f'attachment; filename="{video_id}.{format_ext}"',
                                    "Content-Length": str(file_size),
                                    "Accept-Ranges": "bytes",
                                }
                            )
        except Exception as exc:
            print(f"RapidAPI download failed: {exc}")

    tmpdir = tempfile.mkdtemp(prefix="dl_")
    url = f"https://www.youtube.com/watch?v={video_id}"

    # --- Method 1: pytube ---
    try:
        def pytube_download():
            from pytube import YouTube
            yt = YouTube(url)
            stream = yt.streams.filter(only_audio=True, file_extension="mp4").order_by("abr").desc().first()
            if stream:
                downloaded_file = stream.download(output_path=tmpdir, filename="audio.mp4")
                return downloaded_file, yt.title
            return None, None

        loop = asyncio.get_event_loop()
        downloaded_file, title = await loop.run_in_executor(None, pytube_download)

        if downloaded_file and os.path.exists(downloaded_file):
            output_file = os.path.join(tmpdir, f"output.{format_ext}")
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", downloaded_file, "-q:a", "0", "-map", "a",
                output_file, "-y",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()

            if os.path.exists(output_file):
                try:
                    import shutil
                    shutil.copy2(output_file, cached_file)
                except Exception:
                    pass

                filename = f"{title}.{format_ext}" if title else f"{video_id}.{format_ext}"
                filename = "".join(c for c in filename if ord(c) < 128 or c in " -_.")
                file_size = os.path.getsize(output_file)

                def file_stream_pytube():
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
                        except Exception:
                            pass

                return StreamingResponse(
                    file_stream_pytube(),
                    media_type="audio/mpeg",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Content-Length": str(file_size),
                        "Accept-Ranges": "bytes",
                    }
                )
    except Exception as e:
        print(f"pytube failed: {e}")

    # --- Method 2: yt-dlp ---
    try:
        from yt_dlp import YoutubeDL

        bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin")
        ydl_opts = get_yt_dlp_options(tmpdir, bin_dir, format_ext, quality)

        def download_sync():
            with YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=True)
                    return info.get("title", video_id)
                except Exception as e:
                    error_msg = str(e)
                    if "Sign in" in error_msg or "bot" in error_msg.lower():
                        raise HTTPException(
                            403,
                            "YouTube bot detection triggered. Add a valid cookies.txt to the project root."
                        )
                    raise HTTPException(500, f"yt-dlp extraction failed: {error_msg}")

        loop = asyncio.get_event_loop()
        title = await loop.run_in_executor(None, download_sync)

        files = [f for f in os.listdir(tmpdir) if f.endswith(f".{format_ext}")]
        if files:
            file_path = os.path.join(tmpdir, files[0])
            filename = f"{title}.{format_ext}" if title else files[0]
            filename = "".join(c for c in filename if ord(c) < 128 or c in " -_.")

            try:
                import shutil
                shutil.copy2(file_path, cached_file)
            except Exception:
                pass

            file_size = os.path.getsize(file_path)

            def file_stream_ytdlp():
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

            return StreamingResponse(
                file_stream_ytdlp(),
                media_type="audio/mpeg",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(file_size),
                    "Accept-Ranges": "bytes",
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"yt-dlp failed: {e}")

    try:
        import shutil
        shutil.rmtree(tmpdir)
    except Exception:
        pass

    raise HTTPException(
        503,
        "All download methods failed. Ensure cookies.txt is present and valid, or set RAPIDAPI_KEY."
    )


async def convert_audio(input_file, output_format, tmpdir):
    output_file = os.path.join(tmpdir, f"converted.{output_format}")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", input_file, "-q:a", "0", "-map", "a", output_file, "-y",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    if os.path.exists(output_file):
        try:
            os.remove(input_file)
        except Exception:
            pass
        return output_file
    return input_file


async def cache_file(file_path, video_id, format_ext):
    cached_path = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")
    try:
        import shutil
        shutil.copy2(file_path, cached_path)
        update_manifest(video_id, format_ext)
    except Exception as e:
        print(f"Cache error: {e}")


def update_manifest(video_id, format_ext):
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
            except Exception:
                pass

    return StreamingResponse(
        file_stream(),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
        }
    )


def extract_spotify_playlist_id(link: str) -> str:
    if not link:
        return ""
    if "spotify.com" in link:
        parsed = urlparse(link)
        playlist_id = parsed.path.split("/")[-1].split("?")[0]
        return playlist_id
    elif "spotify:playlist:" in link:
        return link.split(":")[-1]
    return link


async def get_spotify_access_token(client_id: str, client_secret: str) -> str:
    credentials = f"{client_id}:{client_secret}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()
    headers = {
        "Authorization": f"Basic {encoded_credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://accounts.spotify.com/api/token",
            headers=headers,
            data={"grant_type": "client_credentials"}
        )
        if response.status_code == 200:
            return response.json()["access_token"]
        raise HTTPException(500, f"Error getting Spotify token: {response.status_code}")


async def get_cached_spotify_access_token(client_id: str, client_secret: str) -> str:
    now = datetime.utcnow().timestamp()
    if spotify_token_cache["token"] and now < spotify_token_cache["expires_at"] - 60:
        return spotify_token_cache["token"]
    token = await get_spotify_access_token(client_id, client_secret)
    spotify_token_cache["token"] = token
    spotify_token_cache["expires_at"] = now + 3500
    return token


async def fetch_spotify_playlist(playlist_id: str, access_token: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    tracks = []
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    async with httpx.AsyncClient() as client:
        while url:
            params = {
                "limit": 50,
                "fields": "items(added_at,track(id,name,artists(name),album(name,release_date,images),duration_ms,popularity,external_urls)),next",
            }
            response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                data = response.json()
                for item in data["items"]:
                    track = item.get("track")
                    if track:
                        artists = track.get("artists", [])
                        album = track.get("album", {})
                        tracks.append({
                            "name": track.get("name", "N/A"),
                            "artists": [{"name": a["name"]} for a in artists],
                            "album": {
                                "name": album.get("name", "N/A"),
                                "images": album.get("images", []),
                            },
                            "duration_ms": track.get("duration_ms", 0),
                            "popularity": track.get("popularity", 0),
                            "spotify_url": track.get("external_urls", {}).get("spotify", "N/A"),
                            "track_id": track.get("id", "N/A"),
                        })
                url = data.get("next")
            else:
                raise HTTPException(500, f"Error fetching tracks: {response.status_code}")
    return tracks


async def fetch_spotify_playlist_info(playlist_id: str, access_token: str):
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
    import spotify_extractor.cli as spe

    link = request.link

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

    if raw_tracks:
        print(f"DEBUG: First track keys: {list(raw_tracks[0].keys())}")
        print(f"DEBUG: First track: {raw_tracks[0]}")

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
                    ytmusic.search, query, filter="songs", limit=1
                )
                if yt_results:
                    top = yt_results[0]
                    video_id = top.get("videoId")
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
            "duration": duration,
        }

    enriched = await asyncio.gather(*(enrich(t) for t in (raw_tracks or [])))
    tracks = [t for t in enriched if t]

    return {
        "playlistAuthor": playlist_author or "Spotify",
        "playlistName": playlist_name,
        "trackCount": len(tracks),
        "tracks": tracks,
    }


def extract_youtube_playlist_id(link: str) -> str:
    if not link:
        return ""
    parsed = urlparse(link)
    query_params = parse_qs(parsed.query)
    if "list" in query_params:
        return query_params["list"][0]
    return ""


class YouTubePlaylistRequest(BaseModel):
    link: str


@app.post("/youtubePlaylist")
async def youtube_playlist(request: YouTubePlaylistRequest):
    link = request.link
    playlist_id = extract_youtube_playlist_id(link)

    if not playlist_id:
        raise HTTPException(400, "Invalid YouTube playlist link")

    print(f"Fetching YouTube playlist: {playlist_id}")

    try:
        playlist_data = await asyncio.to_thread(
            ytmusic.get_playlist, playlist_id, limit=None
        )

        if not playlist_data:
            raise HTTPException(404, "Playlist not found")

        playlist_name = playlist_data.get("title", "Unknown Playlist")
        playlist_author = (
            playlist_data.get("author", {}).get("name", "Unknown")
            if isinstance(playlist_data.get("author"), dict)
            else playlist_data.get("author", "Unknown")
        )
        playlist_tracks = playlist_data.get("tracks", [])

        print(f"Playlist: {playlist_name} by {playlist_author}, tracks: {len(playlist_tracks)}")

        tracks = []
        for track in playlist_tracks:
            if not track:
                continue
            title = track.get("title", "Unknown")
            video_id = track.get("videoId")
            artists = track.get("artists", [])
            authors = (
                [a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")]
                if isinstance(artists, list) else []
            )
            thumbnails = track.get("thumbnails", [])
            thumbnail = thumbnails[-1].get("url") if thumbnails else None
            duration = track.get("duration")
            tracks.append({
                "title": title,
                "authors": authors,
                "videoId": video_id,
                "thumbnail": thumbnail,
                "duration": duration,
            })

        return {
            "playlistAuthor": playlist_author,
            "playlistName": playlist_name,
            "trackCount": len(tracks),
            "tracks": tracks,
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
    - q: search query (required)
    - type: "all", "songs", "artists", or "albums" (default: "all")
    """
    valid_types = ["all", "songs", "artists", "albums"]
    if type not in valid_types:
        raise HTTPException(400, f"Invalid type. Must be one of: {', '.join(valid_types)}")

    filter_map = {
        "all": None,
        "songs": "songs",
        "artists": "artists",
        "albums": "albums",
    }
    filter_param = filter_map[type]

    try:
        if filter_param:
            results = ytmusic.search(q, filter=filter_param, limit=20)
        else:
            songs = ytmusic.search(q, filter="songs", limit=10)
            artists = ytmusic.search(q, filter="artists", limit=10)
            albums = ytmusic.search(q, filter="albums", limit=10)
            results = {"songs": songs, "artists": artists, "albums": albums}

        if not results:
            raise HTTPException(404, "No results found")

        if type == "all":
            return {
                "songs": format_search_results(results.get("songs", []), "song"),
                "artists": format_search_results(results.get("artists", []), "artist"),
                "albums": format_search_results(results.get("albums", []), "album"),
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
    formatted_results = []
    if not isinstance(items, list):
        return formatted_results

    for item in items:
        if not isinstance(item, dict):
            continue

        if item_type == "song":
            artists = item.get("artists", [])
            artist_names = (
                [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
                if isinstance(artists, list) else []
            )
            formatted_results.append({
                "type": "song",
                "title": item.get("title"),
                "artists": artist_names,
                "duration": item.get("duration"),
                "thumbnail": item.get("thumbnails", [{}])[-1].get("url") if item.get("thumbnails") else None,
                "videoId": item.get("videoId"),
            })

        elif item_type == "artist":
            artist_name = item.get("title") or item.get("name") or item.get("subtitle")
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
                "browseId": item.get("browseId"),
            })

        elif item_type == "album":
            artists = item.get("artists", [])
            artist_names = (
                [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
                if isinstance(artists, list) else []
            )
            formatted_results.append({
                "type": "album",
                "title": item.get("title"),
                "artists": artist_names,
                "year": item.get("year"),
                "thumbnail": item.get("thumbnails", [{}])[-1].get("url") if item.get("thumbnails") else None,
                "browseId": item.get("browseId"),
            })

    return formatted_results


@app.get("/artist/{browseId}")
def get_artist_details(browseId: str):
    try:
        artist_info = ytmusic.get_artist(browseId)
        if not artist_info:
            raise HTTPException(404, "Artist not found")

        response = {
            "name": artist_info.get("name"),
            "description": artist_info.get("description"),
            "thumbnail": artist_info.get("thumbnails", [{}])[-1].get("url") if artist_info.get("thumbnails") else None,
            "browseId": browseId,
        }
        if artist_info.get("songs"):
            response["topSongs"] = format_search_results(artist_info.get("songs", []), "song")
        if artist_info.get("albums"):
            response["albums"] = format_search_results(artist_info.get("albums", []), "album")
        if artist_info.get("singles"):
            response["singles"] = format_search_results(artist_info.get("singles", []), "album")
        return response

    except Exception as e:
        print(f"Error fetching artist details: {e}")
        raise HTTPException(500, f"Failed to fetch artist details: {str(e)}")


@app.get("/album/{browseId}")
def get_album_details(browseId: str):
    try:
        album_info = ytmusic.get_album(browseId)
        if not album_info:
            raise HTTPException(404, "Album not found")

        response = {
            "title": album_info.get("title"),
            "artists": [
                {"name": a.get("name"), "browseId": a.get("id")}
                for a in album_info.get("artists", [])
            ],
            "year": album_info.get("year"),
            "releaseDate": album_info.get("releaseDate"),
            "thumbnail": album_info.get("thumbnails", [{}])[-1].get("url") if album_info.get("thumbnails") else None,
            "browseId": browseId,
        }
        if album_info.get("tracks"):
            response["tracks"] = format_search_results(album_info.get("tracks", []), "song")
        if album_info.get("description"):
            response["description"] = album_info.get("description")
        if album_info.get("duration"):
            response["duration"] = album_info.get("duration")
        return response

    except Exception as e:
        print(f"Error fetching album details: {e}")
        raise HTTPException(500, f"Failed to fetch album details: {str(e)}")


@app.get("/album")
def get_album_songs(browseId: str):
    if not browseId:
        raise HTTPException(400, "browseId query parameter is required")

    try:
        album_info = ytmusic.get_album(browseId)
        if not album_info:
            raise HTTPException(404, "Album not found")

        tracks = album_info.get("tracks", [])
        if not tracks:
            return {
                "browseId": browseId,
                "title": album_info.get("title"),
                "songs": [],
            }

        formatted_tracks = format_search_results(tracks, "song")
        for song in formatted_tracks:
            if not song.get("thumbnail") and song.get("videoId"):
                song["thumbnail"] = f"https://i.ytimg.com/vi/{song['videoId']}/mqdefault.jpg"

        return {
            "browseId": browseId,
            "title": album_info.get("title"),
            "artists": [
                {"name": a.get("name"), "browseId": a.get("id")}
                for a in album_info.get("artists", [])
            ],
            "songs": formatted_tracks,
            "totalSongs": len(formatted_tracks),
        }

    except Exception as e:
        print(f"Error fetching album songs: {e}")
        raise HTTPException(500, f"Failed to fetch album songs: {str(e)}")


class LyricsRequest(BaseModel):
    videoId: Optional[str] = None
    artistName: Optional[str] = None
    trackName: Optional[str] = None


async def fetch_lrclib_lyrics(artist_name: str, track_name: str) -> Optional[dict]:
    params = {"artist_name": artist_name, "track_name": track_name}
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
        return None

    if not data.get("syncedLyrics"):
        return None

    return data


@app.post("/lyrics")
async def get_lyrics(request: LyricsRequest):
    video_id = request.videoId.strip() if request.videoId else None
    artist_name = request.artistName.strip() if request.artistName else None
    track_name = request.trackName.strip() if request.trackName else None

    if not ((artist_name and track_name) or video_id):
        raise HTTPException(400, "artistName and trackName or videoId is required")

    try:
        if artist_name and track_name:
            lrclib_data = await fetch_lrclib_lyrics(artist_name, track_name)
            if lrclib_data:
                return {
                    "syncedLyrics": lrclib_data.get("syncedLyrics"),
                    "source": "lrclib",
                    "returner": "lrclib",
                }

        if not video_id and artist_name and track_name:
            search_query = f"{track_name} {artist_name}"
            search_results = await asyncio.to_thread(
                ytmusic.search, search_query, filter="songs", limit=1
            )
            if search_results:
                video_id = search_results[0].get("videoId")

        if not video_id:
            raise HTTPException(404, "Lyrics not found")

        watch_data = await asyncio.to_thread(ytmusic.get_watch_playlist, video_id)

        if not watch_data or "lyrics" not in watch_data:
            raise HTTPException(404, "Lyrics not available for this song")

        lyrics_browse_id = watch_data["lyrics"]

        try:
            lyrics_data = await asyncio.to_thread(ytmusic.get_lyrics, lyrics_browse_id)
        except Exception as e:
            print(f"get_lyrics failed: {e}")
            raise HTTPException(404, "Lyrics not available for this song")

        if not lyrics_data or "lyrics" not in lyrics_data:
            raise HTTPException(404, "Lyrics not found")

        return {
            "lyrics": lyrics_data["lyrics"],
            "source": lyrics_data.get("source", "YouTube Music"),
            "returner": "ytmusic",
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
        home_data = ytmusic.get_home()
    except Exception as exc:
        raise HTTPException(500, f"Failed to fetch home data: {exc}")

    top = []

    if isinstance(home_data, list):
        for section in home_data:
            if not isinstance(section, dict):
                continue
            contents = section.get("contents", [])
            for item in contents:
                if not isinstance(item, dict):
                    continue
                video_id = item.get("videoId")
                if not video_id:
                    continue
                artists = ", ".join(
                    a.get("name") for a in item.get("artists", []) if a.get("name")
                )
                thumbnails = item.get("thumbnails") or []
                cover = thumbnails[-1].get("url") if thumbnails else None
                top.append({
                    "rank": len(top) + 1,
                    "songName": item.get("title"),
                    "singer": artists,
                    "coverPageUrl": cover,
                    "videoId": video_id,
                })
                if len(top) >= 10:
                    break
            if len(top) >= 10:
                break

    if not top:
        try:
            results = ytmusic.search("trending india songs", filter="songs", limit=10)
            for idx, item in enumerate(results[:10], start=1):
                video_id = item.get("videoId")
                if not video_id:
                    continue
                artists = ", ".join(
                    a.get("name") for a in item.get("artists", []) if a.get("name")
                )
                thumbnails = item.get("thumbnails") or []
                cover = thumbnails[-1].get("url") if thumbnails else None
                top.append({
                    "rank": idx,
                    "songName": item.get("title"),
                    "singer": artists,
                    "coverPageUrl": cover,
                    "videoId": video_id,
                })
        except Exception:
            pass

    if not top:
        raise HTTPException(404, "No chart data found")

    return {"tracks": top}


@app.post("/download/playlist")
async def download_playlist(data: PlaylistDownloadIn):
    """
    Download a playlist as a ZIP of MP3 files.

    Request body:
    {
        "videoIds": ["id1", "id2", ...],
        "quality": 2
    }
    """
    import zipfile
    import shutil

    video_ids = data.videoIds
    quality = data.quality if hasattr(data, "quality") else 2
    format_ext = "mp3"

    if quality not in [1, 2, 3]:
        raise HTTPException(400, "Quality must be 1 (low), 2 (medium), or 3 (high)")
    if not video_ids or not isinstance(video_ids, list):
        raise HTTPException(400, "videoIds must be a non-empty list")
    if len(video_ids) > 100:
        raise HTTPException(400, "Maximum 100 videos per playlist allowed")

    playlist_tmpdir = tempfile.mkdtemp(prefix="playlist_")
    zip_path = os.path.join(playlist_tmpdir, "playlist.zip")

    try:
        downloaded_count = 0
        failed_videos = []

        for idx, video_id in enumerate(video_ids, 1):
            try:
                cached_file = os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")

                if os.path.exists(cached_file):
                    tmpdir_info = tempfile.mkdtemp(prefix="info_")
                    url = f"https://www.youtube.com/watch?v={video_id}"
                    title = video_id

                    try:
                        from yt_dlp import YoutubeDL
                        bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin")
                        ydl_opts = get_yt_dlp_options(tmpdir_info, bin_dir, format_ext, quality)
                        ydl_opts["skip_download"] = True

                        def get_title():
                            with YoutubeDL(ydl_opts) as ydl:
                                try:
                                    info = ydl.extract_info(url, download=False)
                                    return info.get("title", video_id)
                                except Exception:
                                    return video_id

                        loop = asyncio.get_event_loop()
                        title = await loop.run_in_executor(None, get_title)
                    except Exception:
                        title = video_id
                    finally:
                        try:
                            shutil.rmtree(tmpdir_info)
                        except Exception:
                            pass

                    safe_title = "".join(c for c in title if ord(c) < 128 or c in " -_.")
                    dest_path = os.path.join(playlist_tmpdir, f"{idx:03d}_{safe_title}.{format_ext}")
                    shutil.copy2(cached_file, dest_path)
                    downloaded_count += 1

                else:
                    tmpdir = tempfile.mkdtemp(prefix="dl_")
                    url = f"https://www.youtube.com/watch?v={video_id}"

                    try:
                        from yt_dlp import YoutubeDL
                        bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin")
                        ydl_opts = get_yt_dlp_options(tmpdir, bin_dir, format_ext, quality)

                        def download_sync():
                            with YoutubeDL(ydl_opts) as ydl:
                                try:
                                    info = ydl.extract_info(url, download=True)
                                    return info.get("title", video_id)
                                except Exception as e:
                                    error_msg = str(e)
                                    if "403" in error_msg or "429" in error_msg or "bot" in error_msg.lower():
                                        return None
                                    raise Exception(f"yt-dlp extraction failed: {error_msg}")

                        loop = asyncio.get_event_loop()
                        title = await loop.run_in_executor(None, download_sync)

                        if title is None:
                            failed_videos.append(video_id)
                            continue

                        files = [f for f in os.listdir(tmpdir) if f.endswith(f".{format_ext}")]
                        if files:
                            file_path = os.path.join(tmpdir, files[0])
                            safe_title = "".join(c for c in title if ord(c) < 128 or c in " -_.")
                            dest_path = os.path.join(playlist_tmpdir, f"{idx:03d}_{safe_title}.{format_ext}")
                            shutil.copy2(file_path, dest_path)
                            try:
                                shutil.copy2(file_path, os.path.join(CACHE_DIR, f"{video_id}.{format_ext}"))
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

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            files = sorted([f for f in os.listdir(playlist_tmpdir) if f.endswith(f".{format_ext}")])
            for file in files:
                file_path = os.path.join(playlist_tmpdir, file)
                arcname = file.split("_", 1)[1] if "_" in file else file
                zipf.write(file_path, arcname)

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
                "Content-Disposition": 'attachment; filename="playlist.zip"',
                "Content-Length": str(zip_size),
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        try:
            import shutil
            shutil.rmtree(playlist_tmpdir)
        except Exception:
            pass
        raise HTTPException(500, f"Playlist download failed: {str(e)}")