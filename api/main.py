from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse, Response, JSONResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic
import subprocess
import os
import tempfile
import asyncio
import json
import random
import uuid
import time
import logging
import platform
import re
import shutil
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
import httpx
import hmac
import hashlib
import base64
from urllib.parse import urlparse, parse_qs
from typing import Optional, Callable, Dict, Any, List, Tuple

# Load .local.env if it exists
env_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".local.env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,80}$")


def _int_env(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _float_env(name: str, default: float, minimum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


WRITABLE_COOKIES = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
COOKIES_SOURCES = [
    "/etc/secrets/cookies.txt",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cookies.txt"),
]

DOWNLOAD_WORKER_COUNT = _int_env("DOWNLOAD_WORKER_COUNT", 2, 1)
DOWNLOAD_QUEUE_MAXSIZE = _int_env("DOWNLOAD_QUEUE_MAXSIZE", 200, 10)
DOWNLOAD_MAX_RETRIES = _int_env("DOWNLOAD_MAX_RETRIES", 4, 1)
DOWNLOAD_BACKOFF_BASE_SECONDS = _float_env("DOWNLOAD_BACKOFF_BASE_SECONDS", 1.5, 0.1)
DOWNLOAD_BACKOFF_MAX_SECONDS = _float_env("DOWNLOAD_BACKOFF_MAX_SECONDS", 45, 1)
DOWNLOAD_JOB_TTL_SECONDS = _int_env("DOWNLOAD_JOB_TTL_SECONDS", 3600, 60)
DOWNLOAD_PROVIDER_TIMEOUT_SECONDS = _int_env("DOWNLOAD_PROVIDER_TIMEOUT_SECONDS", 120, 10)
DOWNLOAD_UPSTREAM_CONCURRENCY = _int_env("DOWNLOAD_UPSTREAM_CONCURRENCY", 2, 1)
DOWNLOAD_SYNC_WAIT_SECONDS = _int_env("DOWNLOAD_SYNC_WAIT_SECONDS", 25, 5)
DOWNLOAD_RATE_LIMIT_WINDOW_SECONDS = _int_env("DOWNLOAD_RATE_LIMIT_WINDOW_SECONDS", 60, 1)
DOWNLOAD_RATE_LIMIT_MAX_REQUESTS = _int_env("DOWNLOAD_RATE_LIMIT_MAX_REQUESTS", 30, 1)
CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(PROJECT_ROOT, "downloads"))
CACHE_RETENTION_SECONDS = _int_env("CACHE_RETENTION_SECONDS", 86400, 300)
CACHE_CLEANUP_INTERVAL_SECONDS = _int_env("CACHE_CLEANUP_INTERVAL_SECONDS", 900, 60)
CACHE_MAX_BYTES = _int_env("CACHE_MAX_BYTES", 2 * 1024 * 1024 * 1024, 10 * 1024 * 1024)
CACHE_CLEANUP_ON_START = _bool_env("CACHE_CLEANUP_ON_START", False)
ENABLE_DEPLOY_WEBHOOK = _bool_env("ENABLE_DEPLOY_WEBHOOK", False)
YOUTUBEI_SERVICE_URL = os.getenv("YOUTUBEI_SERVICE_URL", "http://localhost:3001")
YOUTUBEI_SERVICE_TIMEOUT_SECONDS = _int_env("YOUTUBEI_SERVICE_TIMEOUT_SECONDS", 20, 1)
YOUTUBEI_SERVICE_RETRIES = _int_env("YOUTUBEI_SERVICE_RETRIES", 3, 1)

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        skip_keys = {
            "args", "asctime", "created", "exc_info", "exc_text", "filename",
            "funcName", "levelname", "levelno", "lineno", "module", "msecs",
            "message", "msg", "name", "pathname", "process", "processName",
            "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        }
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in skip_keys:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = str(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging():
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


configure_logging()
logger = logging.getLogger("yuzone")


def refresh_cookies() -> bool:
    for candidate in COOKIES_SOURCES:
        if os.path.exists(candidate):
            try:
                shutil.copy2(candidate, WRITABLE_COOKIES)
                logger.info("cookies refreshed", extra={"source": candidate})
                return True
            except Exception as e:
                logger.warning("cookie refresh failed", extra={"source": candidate, "error": str(e)})
    logger.warning("cookies file not found")
    return False


async def cookie_refresh_loop():
    while True:
        refresh_cookies()
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresh_cookies()
    log_startup_diagnostics()
    if CACHE_CLEANUP_ON_START:
        run_cache_cleanup()
    cookie_task = asyncio.create_task(cookie_refresh_loop())
    gc_task = asyncio.create_task(download_job_gc_loop())
    cache_task = asyncio.create_task(cache_cleanup_loop())

    worker_tasks = [
        asyncio.create_task(download_worker(f"dl-worker-{idx+1}"))
        for idx in range(max(1, DOWNLOAD_WORKER_COUNT))
    ]

    yield

    cookie_task.cancel()
    gc_task.cancel()
    cache_task.cancel()
    for task in worker_tasks:
        task.cancel()

    try:
        await cookie_task
    except asyncio.CancelledError:
        pass

    try:
        await gc_task
    except asyncio.CancelledError:
        pass

    try:
        await cache_task
    except asyncio.CancelledError:
        pass

    for task in worker_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


def prepare_binary_paths() -> str:
    """Ensure bundled binaries are executable and discoverable in serverless runtimes."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bin_dir = os.path.join(project_root, "bin")

    if os.path.isdir(bin_dir):
        for name in ("ffmpeg", "ffprobe"):
            binary_path = os.path.join(bin_dir, name)
            if os.path.isfile(binary_path):
                try:
                    os.chmod(binary_path, 0o755)
                except OSError:
                    # Ignore chmod failures on non-POSIX systems.
                    pass

        current_path = os.environ.get("PATH", "")
        if bin_dir not in current_path.split(os.pathsep):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{current_path}" if current_path else bin_dir

    return bin_dir


BIN_DIR = prepare_binary_paths()


def is_serverless_runtime() -> bool:
    """Detect serverless environments where chunk streaming can be unreliable."""
    return bool(
        os.getenv("NETLIFY")
        or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        or os.getenv("LAMBDA_TASK_ROOT")
    )


def build_file_response(
    file_path: str,
    filename: str,
    media_type: str,
    cleanup: Optional[Callable[[], None]] = None,
    include_accept_ranges: bool = True,
):
    """Return streaming response locally; return buffered response on serverless runtimes."""
    file_size = os.path.getsize(file_path)
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(file_size),
    }
    if include_accept_ranges:
        headers["Accept-Ranges"] = "bytes"

    if is_serverless_runtime():
        with open(file_path, "rb") as f:
            payload = f.read()
        if cleanup:
            cleanup()
        return Response(content=payload, media_type=media_type, headers=headers)

    def file_stream():
        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            if cleanup:
                cleanup()

    return StreamingResponse(file_stream(), media_type=media_type, headers=headers)

app = FastAPI(lifespan=lifespan)

cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=os.getenv("CORS_ALLOW_CREDENTIALS", "false").lower() == "true",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
ytmusic = YTMusic()


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "request failed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
                "client_ip": _client_ip(request),
            },
        )
        raise

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["x-request-id"] = request_id
    logger.info(
        "request completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "client_ip": _client_ip(request),
        },
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    detail = exc.detail
    if not isinstance(detail, dict):
        detail = {"message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "unhandled application error",
        extra={"path": request.url.path, "method": request.method, "error": str(exc)[:300]},
    )
    return JSONResponse(
        status_code=500,
        content={"error": {"category": "internal_error", "message": "Internal server error"}},
    )

# Simple in-memory cache for Spotify client credentials tokens
spotify_token_cache = {
    "token": None,
    "expires_at": 0.0  # unix timestamp
}

# allow at most 5 downloads running at the same time
download_semaphore = asyncio.Semaphore(5)

# Setup cache directory
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MANIFEST = os.path.join(CACHE_DIR, "manifest.json")

download_queue: asyncio.Queue = asyncio.Queue(maxsize=max(10, DOWNLOAD_QUEUE_MAXSIZE))
download_jobs: Dict[str, Dict[str, Any]] = {}
download_jobs_by_key: Dict[str, str] = {}
download_jobs_lock = asyncio.Lock()
download_upstream_semaphore = asyncio.Semaphore(max(1, DOWNLOAD_UPSTREAM_CONCURRENCY))

request_rate_limit: Dict[str, List[float]] = {}
request_rate_limit_lock = asyncio.Lock()

download_metrics: Dict[str, Any] = {
    "queued_total": 0,
    "completed_total": 0,
    "failed_total": 0,
    "in_progress": 0,
    "provider_attempts": {
        "youtubei": 0,
        "rapidapi": 0,
        "yt-dlp": 0,
        "pytube": 0,
    },
    "provider_success": {
        "youtubei": 0,
        "rapidapi": 0,
        "yt-dlp": 0,
        "pytube": 0,
    },
    "rate_limited_total": 0,
    "queue_rejected_total": 0,
}


class DownloadIn(BaseModel):
    videoId: str
    quality: int = 2  # 1=low, 2=medium, 3=high
    # Format is always MP3 - no other formats supported


class PlaylistDownloadIn(BaseModel):
    videoIds: list
    quality: int = 2  # 1=low, 2=medium, 3=high
    # Format is always MP3 - no other formats supported


class DownloadPipelineError(Exception):
    def __init__(
        self,
        message: str,
        category: str = "download_error",
        provider: Optional[str] = None,
        status_code: int = 503,
        retryable: bool = True,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.provider:
            payload["provider"] = self.provider
        if self.details:
            payload["details"] = self.details
        return payload


@app.exception_handler(DownloadPipelineError)
async def download_pipeline_exception_handler(_request: Request, exc: DownloadPipelineError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.to_dict()})


def validate_video_id(video_id: str) -> str:
    value = (video_id or "").strip()
    if not VIDEO_ID_RE.match(value):
        raise HTTPException(400, "videoId must be an 11-character YouTube video ID")
    return value


def validate_quality(quality: int) -> int:
    if quality not in [1, 2, 3]:
        raise HTTPException(400, "Quality must be 1 (low), 2 (medium), or 3 (high)")
    return quality


def classify_download_error(error: Any) -> DownloadPipelineError:
    if isinstance(error, DownloadPipelineError):
        return error

    message = str(error)
    normalized = message.lower()
    category = "download_error"
    status_code = 503
    retryable = True

    if "429" in normalized or "too many requests" in normalized:
        category = "youtube_rate_limited"
        status_code = 429
    elif "sign in to confirm" in normalized or "not a bot" in normalized or "confirm you're not a bot" in normalized:
        category = "youtube_bot_check"
        status_code = 403
    elif "cookies are no longer valid" in normalized or "cookie" in normalized and "invalid" in normalized:
        category = "youtube_cookie_failure"
        status_code = 403
    elif "geo" in normalized or "not available in your country" in normalized or "country" in normalized:
        category = "geo_restricted"
        status_code = 451
        retryable = False
    elif "age" in normalized and ("restricted" in normalized or "confirm" in normalized):
        category = "age_restricted"
        status_code = 403
        retryable = False
    elif "unavailable" in normalized or "private video" in normalized or "removed" in normalized:
        category = "video_unavailable"
        status_code = 404
        retryable = False
    elif "ffmpeg" in normalized or "conversion" in normalized:
        category = "ffmpeg_failure"
        status_code = 500
    elif "timed out" in normalized or "timeout" in normalized:
        category = "download_timeout"
        status_code = 504

    return DownloadPipelineError(
        message=message[:400],
        category=category,
        status_code=status_code,
        retryable=retryable,
    )


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


def get_yt_dlp_options(
    tmpdir: str,
    bin_dir: str,
    format_ext: str,
    quality: int,
    player_clients: Optional[List[str]] = None,
    use_cookies: bool = False,
) -> dict:
    """
    Generate optimized yt-dlp options to handle 403 errors and bot detection.
    """
    quality_settings = get_quality_settings(quality)
    player_clients = player_clients or ['web']

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
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': min(30, DOWNLOAD_PROVIDER_TIMEOUT_SECONDS),
        'ffmpeg_location': bin_dir,
        'keepvideo': False,
        'noplaylist': True,
        'cachedir': False,
        'ignoreerrors': False,
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
        'retries': max(3, DOWNLOAD_MAX_RETRIES),
        'skip_unavailable_fragments': True,
        'nocheckcertificate': True,
        'prefer_insecure': False,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        'extractor_args': {
            'youtube': {
                'player_client': player_clients,
                'lang': ['en'],
            }
        },
    }

    # Add cookies if available - critical for reducing 403 errors.
    # Prefer the refreshed temp copy so deployments can use mounted secrets.
    if use_cookies:
        cookies_file = _resolve_cookiefile()
        if cookies_file:
            opts['cookiefile'] = cookies_file

    return opts


def _resolve_cookiefile() -> Optional[str]:
    cookies_candidates = [
        WRITABLE_COOKIES,
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cookies.txt'),
    ]
    for cookies_file in cookies_candidates:
        if os.path.exists(cookies_file):
            return cookies_file
    return None


def _is_cookie_or_bot_block_error(error_message: str) -> bool:
    normalized = error_message.lower()
    return (
        'sign in to confirm' in normalized
        or 'cookies are no longer valid' in normalized
        or 'not a bot' in normalized
        or '403' in normalized
        or '429' in normalized
    )


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _download_job_key(video_id: str, quality: int) -> str:
    return f"{video_id}:{quality}:mp3"


def _calculate_backoff_seconds(attempt: int) -> float:
    base = DOWNLOAD_BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
    capped = min(base, DOWNLOAD_BACKOFF_MAX_SECONDS)
    return capped + random.uniform(0.0, min(1.0, capped * 0.2))


def _client_ip(request: Optional[Request]) -> str:
    if request is None:
        return "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def _enforce_download_rate_limit(client_ip: str):
    now = time.monotonic()
    cutoff = now - DOWNLOAD_RATE_LIMIT_WINDOW_SECONDS

    async with request_rate_limit_lock:
        events = request_rate_limit.setdefault(client_ip, [])
        events[:] = [event for event in events if event >= cutoff]

        if len(events) >= DOWNLOAD_RATE_LIMIT_MAX_REQUESTS:
            async with download_jobs_lock:
                download_metrics["rate_limited_total"] += 1
            raise HTTPException(429, "Too many download requests from this client. Please retry shortly.")

        events.append(now)


def _sanitize_filename(raw_name: str, default_name: str) -> str:
    safe = "".join(c for c in (raw_name or "") if ord(c) < 128 or c in " -_.")
    safe = safe.strip() or default_name
    if not safe.lower().endswith(".mp3"):
        safe = f"{safe}.mp3"
    return safe


async def _mark_job_failed(job: Dict[str, Any], error_message: Any):
    error_payload: Any = error_message
    if isinstance(error_message, DownloadPipelineError):
        error_payload = error_message.to_dict()
    async with download_jobs_lock:
        job["status"] = "failed"
        job["error"] = error_payload if isinstance(error_payload, dict) else str(error_payload)[:400]
        job["updatedAt"] = _utc_now_iso()
        download_metrics["failed_total"] += 1


async def _finalize_job_success(job: Dict[str, Any], title: str):
    video_id = job["videoId"]
    cache_path = os.path.join(CACHE_DIR, f"{video_id}.mp3")
    filename = _sanitize_filename(title, f"{video_id}.mp3")

    async with download_jobs_lock:
        job["status"] = "completed"
        job["error"] = None
        job["cachePath"] = cache_path
        job["filename"] = filename
        job["updatedAt"] = _utc_now_iso()
        download_metrics["completed_total"] += 1


async def _call_youtubei_extract(video_id: str) -> Dict[str, Any]:
    endpoint = f"{YOUTUBEI_SERVICE_URL.rstrip('/')}/extract/{video_id}"
    last_error = "youtubei service call failed"

    for attempt in range(1, max(1, YOUTUBEI_SERVICE_RETRIES) + 1):
        try:
            async with httpx.AsyncClient(timeout=YOUTUBEI_SERVICE_TIMEOUT_SECONDS) as client:
                response = await client.get(endpoint)
                if response.status_code == 200:
                    payload = response.json()
                    if not payload.get("audioUrl"):
                        raise RuntimeError("youtubei service response missing audioUrl")
                    return payload

                if response.status_code in (404, 422):
                    raise RuntimeError(f"youtubei no stream found ({response.status_code})")

                last_error = f"youtubei status={response.status_code} body={response.text[:180]}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < YOUTUBEI_SERVICE_RETRIES:
            await asyncio.sleep(_calculate_backoff_seconds(attempt))

    raise RuntimeError(last_error)


async def _download_via_youtubei_service(video_id: str, quality: int) -> str:
    quality_settings = get_quality_settings(quality)
    tmpdir = tempfile.mkdtemp(prefix="youtubei_dl_")
    raw_file = os.path.join(tmpdir, f"{video_id}.raw")
    output_file = os.path.join(tmpdir, f"{video_id}.mp3")
    cache_path = os.path.join(CACHE_DIR, f"{video_id}.mp3")

    try:
        payload = await _call_youtubei_extract(video_id)
        audio_url = payload.get("audioUrl")

        async with httpx.AsyncClient(timeout=YOUTUBEI_SERVICE_TIMEOUT_SECONDS) as client:
            async with client.stream("GET", audio_url) as stream_response:
                stream_response.raise_for_status()
                with open(raw_file, "wb") as target:
                    async for chunk in stream_response.aiter_bytes(65536):
                        target.write(chunk)

        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            raw_file,
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            quality_settings["bitrate"],
            output_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0 or not os.path.exists(output_file):
            err_preview = (stderr.decode("utf-8", errors="ignore") if stderr else "")[:220]
            raise RuntimeError(f"ffmpeg conversion failed for youtubei stream: {err_preview}")

        import shutil
        shutil.copy2(output_file, cache_path)
        update_manifest(video_id, "mp3")

        return payload.get("title") or video_id
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir)
        except Exception:
            pass


async def _check_youtubei_health() -> Dict[str, Any]:
    endpoint = f"{YOUTUBEI_SERVICE_URL.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(endpoint)
            if response.status_code != 200:
                return {"ok": False, "status": response.status_code}
            payload = response.json()
            return {"ok": True, "status": response.status_code, "service": payload}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def _command_version(command: List[str], timeout: int = 8) -> Dict[str, Any]:
    executable = shutil.which(command[0])
    if not executable:
        return {"ok": False, "error": f"{command[0]} not found on PATH"}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        output = (result.stdout or result.stderr or "").splitlines()
        return {
            "ok": result.returncode == 0,
            "executable": executable,
            "returnCode": result.returncode,
            "version": output[0][:300] if output else "",
        }
    except Exception as exc:
        return {"ok": False, "executable": executable, "error": str(exc)[:300]}


def check_ffmpeg() -> Dict[str, Any]:
    return _command_version(["ffmpeg", "-version"])


def check_ytdlp() -> Dict[str, Any]:
    return _command_version([sys.executable, "-m", "yt_dlp", "--version"])


def check_cache_writable() -> Dict[str, Any]:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        probe = os.path.join(CACHE_DIR, f".write-test-{uuid.uuid4().hex}")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return {"ok": True, "path": os.path.abspath(CACHE_DIR)}
    except Exception as exc:
        return {"ok": False, "path": os.path.abspath(CACHE_DIR), "error": str(exc)[:300]}


def dependency_diagnostics() -> Dict[str, Any]:
    return {
        "ffmpeg": check_ffmpeg(),
        "ytDlp": check_ytdlp(),
        "cache": check_cache_writable(),
        "cookies": {
            "configured": bool(_resolve_cookiefile()),
            "writableCopyExists": os.path.exists(WRITABLE_COOKIES),
        },
    }


def log_startup_diagnostics():
    logger.info(
        "startup diagnostics",
        extra={
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cache_dir": os.path.abspath(CACHE_DIR),
            "download_worker_count": DOWNLOAD_WORKER_COUNT,
            "download_upstream_concurrency": DOWNLOAD_UPSTREAM_CONCURRENCY,
            "youtubei_service_url": YOUTUBEI_SERVICE_URL,
            "dependencies": dependency_diagnostics(),
        },
    )


def run_cache_cleanup() -> Dict[str, Any]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    now = time.time()
    removed_files = 0
    removed_bytes = 0
    entries: List[Tuple[float, str]] = []

    for path in Path(CACHE_DIR).glob("*.mp3"):
        try:
            stat = path.stat()
        except OSError:
            continue
        age = now - stat.st_mtime
        if age > CACHE_RETENTION_SECONDS:
            removed_bytes += stat.st_size
            path.unlink(missing_ok=True)
            removed_files += 1
        else:
            entries.append((stat.st_mtime, str(path)))

    total_bytes = sum(Path(path).stat().st_size for _, path in entries if Path(path).exists())
    for _, path in sorted(entries, key=lambda item: item[0]):
        if total_bytes <= CACHE_MAX_BYTES:
            break
        try:
            stat = os.stat(path)
            os.remove(path)
            total_bytes -= stat.st_size
            removed_bytes += stat.st_size
            removed_files += 1
        except OSError:
            pass

    return {"removedFiles": removed_files, "removedBytes": removed_bytes, "remainingBytes": total_bytes}


async def cache_cleanup_loop():
    while True:
        await asyncio.sleep(CACHE_CLEANUP_INTERVAL_SECONDS)
        summary = await asyncio.to_thread(run_cache_cleanup)
        if summary["removedFiles"]:
            logger.info("cache cleanup completed", extra=summary)


async def _download_via_rapidapi(video_id: str, quality: int) -> str:
    rapidapi_key = os.getenv("RAPIDAPI_KEY")
    if not rapidapi_key:
        raise RuntimeError("RAPIDAPI_KEY not configured")

    rapidapi_host = "youtube-media-downloader.p.rapidapi.com"
    quality_settings = get_quality_settings(quality)
    tmpdir = tempfile.mkdtemp(prefix="rapidapi_dl_")
    raw_file = os.path.join(tmpdir, f"{video_id}.raw")
    output_file = os.path.join(tmpdir, f"{video_id}.mp3")
    cache_path = os.path.join(CACHE_DIR, f"{video_id}.mp3")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"https://{rapidapi_host}/v2/video/streams",
                params={"videoId": video_id},
                headers={
                    "x-rapidapi-key": rapidapi_key,
                    "x-rapidapi-host": rapidapi_host,
                },
            )

            if resp.status_code != 200:
                raise RuntimeError(f"RapidAPI stream lookup failed with {resp.status_code}")

            payload = resp.json()
            streams = payload.get("streams") or payload.get("formats") or []
            audio_streams = []
            for stream in streams:
                mime = (stream.get("mimeType") or stream.get("type") or "").lower()
                if "audio" in mime:
                    audio_streams.append(stream)

            if not audio_streams:
                raise RuntimeError("RapidAPI did not return an audio stream")

            audio_streams.sort(key=lambda x: x.get("bitrate") or x.get("kbps") or 0, reverse=True)
            audio_url = audio_streams[0].get("url") or audio_streams[0].get("downloadUrl")
            if not audio_url:
                raise RuntimeError("RapidAPI audio stream missing URL")

            async with client.stream("GET", audio_url) as dresp:
                dresp.raise_for_status()
                with open(raw_file, "wb") as out:
                    async for chunk in dresp.aiter_bytes(65536):
                        out.write(chunk)

        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            raw_file,
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            quality_settings["bitrate"],
            output_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        if process.returncode != 0 or not os.path.exists(output_file):
            raise RuntimeError("RapidAPI audio conversion failed")

        import shutil
        shutil.copy2(output_file, cache_path)
        update_manifest(video_id, "mp3")

        return video_id
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir)
        except Exception:
            pass


async def _download_via_yt_dlp(video_id: str, quality: int) -> str:
    cache_path = os.path.join(CACHE_DIR, f"{video_id}.mp3")
    url = f"https://www.youtube.com/watch?v={video_id}"
    strategy_profiles = [
        ("primary-web", ["web"], False),
        ("android-client", ["android"], False),
        ("alt-web-clients", ["mweb", "ios", "web"], False),
        ("cookie-web", ["web", "mweb"], True),
    ]

    last_error = "yt-dlp failed"

    for profile_name, clients, use_cookies in strategy_profiles:
        tmpdir = tempfile.mkdtemp(prefix=f"ytdlp_{profile_name}_")
        try:
            def sync_download() -> tuple:
                from yt_dlp import YoutubeDL

                opts = get_yt_dlp_options(
                    tmpdir,
                    BIN_DIR,
                    "mp3",
                    quality,
                    player_clients=clients,
                    use_cookies=use_cookies,
                )

                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", video_id)

                files = [f for f in os.listdir(tmpdir) if f.endswith(".mp3")]
                if not files:
                    raise RuntimeError("yt-dlp did not produce MP3 output")

                return os.path.join(tmpdir, files[0]), title

            produced_file, title = await asyncio.wait_for(
                asyncio.to_thread(sync_download),
                timeout=DOWNLOAD_PROVIDER_TIMEOUT_SECONDS,
            )

            import shutil
            shutil.copy2(produced_file, cache_path)
            update_manifest(video_id, "mp3")
            logger.info("yt-dlp profile succeeded", extra={"profile": profile_name, "video_id": video_id})
            return title
        except Exception as profile_error:
            classified = classify_download_error(profile_error)
            classified.provider = "yt-dlp"
            last_error = f"{profile_name}: {classified.message}"
            logger.warning(
                "yt-dlp profile failed",
                extra={
                    "profile": profile_name,
                    "video_id": video_id,
                    "category": classified.category,
                    "retryable": classified.retryable,
                    "error": classified.message[:200],
                },
            )
            if not classified.retryable:
                raise classified
        finally:
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass

    raise classify_download_error(last_error)


async def _download_via_pytube(video_id: str, quality: int) -> str:
    tmpdir = tempfile.mkdtemp(prefix="pytube_dl_")
    source_file = os.path.join(tmpdir, "audio.mp4")
    output_file = os.path.join(tmpdir, "audio.mp3")
    cache_path = os.path.join(CACHE_DIR, f"{video_id}.mp3")
    url = f"https://www.youtube.com/watch?v={video_id}"
    quality_settings = get_quality_settings(quality)

    try:
        def sync_download() -> str:
            from pytube import YouTube

            yt = YouTube(url)
            stream = yt.streams.filter(only_audio=True, file_extension="mp4").order_by("abr").desc().first()
            if not stream:
                raise RuntimeError("pytube did not find an audio stream")
            stream.download(output_path=tmpdir, filename="audio")
            return yt.title or video_id

        title = await asyncio.to_thread(sync_download)

        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            source_file,
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            quality_settings["bitrate"],
            output_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        if process.returncode != 0 or not os.path.exists(output_file):
            raise RuntimeError("pytube conversion to mp3 failed")

        import shutil
        shutil.copy2(output_file, cache_path)
        update_manifest(video_id, "mp3")
        return title
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir)
        except Exception:
            pass


async def process_download_job(job_id: str):
    async with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            return
        if job["status"] in {"completed", "failed", "running"}:
            return
        job["status"] = "running"
        job["updatedAt"] = _utc_now_iso()
        download_metrics["in_progress"] += 1

    video_id = job["videoId"]
    quality = job["quality"]
    cache_path = os.path.join(CACHE_DIR, f"{video_id}.mp3")

    try:
        if os.path.exists(cache_path):
            await _finalize_job_success(job, video_id)
            return

        providers = [
            ("youtubei", _download_via_youtubei_service),
            ("rapidapi", _download_via_rapidapi),
            ("pytube", _download_via_pytube),
            ("yt-dlp", _download_via_yt_dlp),
        ]

        last_error: Any = "download failed"
        for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
            async with download_jobs_lock:
                job["attempt"] = attempt
                job["updatedAt"] = _utc_now_iso()

            for provider_name, provider in providers:
                async with download_jobs_lock:
                    download_metrics["provider_attempts"][provider_name] += 1
                    job["providersTried"].append(provider_name)

                try:
                    async with download_upstream_semaphore:
                        title = await asyncio.wait_for(
                            provider(video_id, quality),
                            timeout=DOWNLOAD_PROVIDER_TIMEOUT_SECONDS,
                        )
                    async with download_jobs_lock:
                        download_metrics["provider_success"][provider_name] += 1
                    await _finalize_job_success(job, title)
                    return
                except Exception as provider_error:
                    classified = classify_download_error(provider_error)
                    classified.provider = provider_name
                    last_error = classified
                    logger.warning(
                        "download provider failed",
                        extra={
                            "provider": provider_name,
                            "video_id": video_id,
                            "attempt": attempt,
                            "category": classified.category,
                            "retryable": classified.retryable,
                            "error": classified.message[:240],
                        },
                    )
                    if not classified.retryable:
                        await _mark_job_failed(job, classified)
                        return

            if attempt < DOWNLOAD_MAX_RETRIES:
                backoff = _calculate_backoff_seconds(attempt)
                async with download_jobs_lock:
                    job["nextRetryInSeconds"] = round(backoff, 2)
                    job["lastError"] = last_error.to_dict() if isinstance(last_error, DownloadPipelineError) else str(last_error)[:400]
                    job["updatedAt"] = _utc_now_iso()
                await asyncio.sleep(backoff)

        await _mark_job_failed(job, last_error)
    finally:
        async with download_jobs_lock:
            download_metrics["in_progress"] = max(0, download_metrics["in_progress"] - 1)


async def download_worker(worker_name: str):
    while True:
        job_id = await download_queue.get()
        try:
            await process_download_job(job_id)
        except Exception as worker_error:
            logger.exception("worker failure", extra={"worker": worker_name, "job_id": job_id, "error": str(worker_error)[:200]})
        finally:
            download_queue.task_done()


async def download_job_gc_loop():
    while True:
        await asyncio.sleep(300)
        cutoff = datetime.utcnow().timestamp() - DOWNLOAD_JOB_TTL_SECONDS

        async with download_jobs_lock:
            removable = []
            for job_id, job in download_jobs.items():
                if job["status"] not in {"completed", "failed"}:
                    continue
                try:
                    updated_ts = datetime.fromisoformat(job["updatedAt"].replace("Z", "")).timestamp()
                except Exception:
                    updated_ts = 0
                if updated_ts < cutoff:
                    removable.append((job_id, _download_job_key(job["videoId"], job["quality"])))

            for job_id, key in removable:
                download_jobs.pop(job_id, None)
                if download_jobs_by_key.get(key) == job_id:
                    download_jobs_by_key.pop(key, None)


async def enqueue_download_job(video_id: str, quality: int) -> Dict[str, Any]:
    key = _download_job_key(video_id, quality)
    now_iso = _utc_now_iso()

    cached_file = os.path.join(CACHE_DIR, f"{video_id}.mp3")
    if os.path.exists(cached_file):
        job_id = uuid.uuid4().hex
        ready_job = {
            "jobId": job_id,
            "videoId": video_id,
            "quality": quality,
            "status": "completed",
            "attempt": 0,
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "cachePath": cached_file,
            "filename": f"{video_id}.mp3",
            "providersTried": [],
            "lastError": None,
            "nextRetryInSeconds": 0,
            "error": None,
        }
        async with download_jobs_lock:
            download_jobs[job_id] = ready_job
        return ready_job

    async with download_jobs_lock:
        existing_job_id = download_jobs_by_key.get(key)
        if existing_job_id and existing_job_id in download_jobs:
            existing_job = download_jobs[existing_job_id]
            if existing_job["status"] in {"queued", "running", "completed"}:
                return existing_job

        if download_queue.full():
            download_metrics["queue_rejected_total"] += 1
            raise HTTPException(503, "Download queue is full. Please retry shortly.")

        job_id = uuid.uuid4().hex
        job = {
            "jobId": job_id,
            "videoId": video_id,
            "quality": quality,
            "status": "queued",
            "attempt": 0,
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "cachePath": None,
            "filename": f"{video_id}.mp3",
            "providersTried": [],
            "lastError": None,
            "nextRetryInSeconds": 0,
            "error": None,
        }

        download_jobs[job_id] = job
        download_jobs_by_key[key] = job_id
        download_metrics["queued_total"] += 1

    await download_queue.put(job_id)
    return job



@app.get("/")
def root():
    return RedirectResponse(url="/top")


@app.get("/health")
def health():
    return {"status": "ok", "service": "yuzone-api", "uptime": "running"}


@app.get("/ready")
def ready():
    diagnostics = dependency_diagnostics()
    ready_ok = all(component.get("ok") for component in [
        diagnostics["ffmpeg"],
        diagnostics["ytDlp"],
        diagnostics["cache"],
    ])
    return JSONResponse(
        status_code=200 if ready_ok else 503,
        content={"status": "ready" if ready_ok else "not_ready", "checks": diagnostics},
    )


@app.get("/debug/system")
def debug_system():
    diagnostics = dependency_diagnostics()
    return {
        "python": {
            "version": sys.version,
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "dependencies": diagnostics,
        "runtime": {
            "cacheDir": os.path.abspath(CACHE_DIR),
            "cacheRetentionSeconds": CACHE_RETENTION_SECONDS,
            "cacheMaxBytes": CACHE_MAX_BYTES,
            "downloadWorkerCount": DOWNLOAD_WORKER_COUNT,
            "downloadUpstreamConcurrency": DOWNLOAD_UPSTREAM_CONCURRENCY,
            "youtubeiServiceConfigured": bool(YOUTUBEI_SERVICE_URL),
        },
    }


@app.get("/debug/ytdlp")
async def debug_ytdlp():
    test_video_id = os.getenv("YT_DLP_DIAGNOSTIC_VIDEO_ID", "dQw4w9WgXcQ")
    url = f"https://www.youtube.com/watch?v={test_video_id}"

    def extract_probe():
        from yt_dlp import YoutubeDL

        extractor_args = {
            "videoId": test_video_id,
            "url": url,
            "playerClients": ["web"],
            "cookiesUsed": False,
            "downloadAttempted": False,
        }
        opts = get_yt_dlp_options(
            tempfile.gettempdir(),
            BIN_DIR,
            "mp3",
            1,
            player_clients=["web"],
            use_cookies=False,
        )
        opts.update({
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        })
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats") or []
            audio_formats = [
                fmt for fmt in formats
                if fmt.get("acodec") not in (None, "none")
            ]
            video_formats = [
                fmt for fmt in formats
                if fmt.get("vcodec") not in (None, "none")
            ]
            audio_only_formats = [
                fmt for fmt in audio_formats
                if fmt.get("vcodec") in (None, "none")
            ]
            best_audio = max(
                audio_formats,
                key=lambda fmt: fmt.get("abr") or fmt.get("tbr") or 0,
                default={},
            )

            return {
                "acceptedByYouTube": True,
                "id": info.get("id"),
                "title": info.get("title"),
                "duration": info.get("duration"),
                "extractor": info.get("extractor"),
                "extractorKey": info.get("extractor_key"),
                "availability": info.get("availability"),
                "ageLimit": info.get("age_limit"),
                "liveStatus": info.get("live_status"),
                "channel": info.get("channel"),
                "webpageUrl": info.get("webpage_url"),
                "formatCount": len(info.get("formats") or []),
                "audioFormatCount": len(audio_formats),
                "audioOnlyFormatCount": len(audio_only_formats),
                "videoFormatCount": len(video_formats),
                "bestAudio": {
                    "formatId": best_audio.get("format_id"),
                    "ext": best_audio.get("ext"),
                    "abr": best_audio.get("abr"),
                    "tbr": best_audio.get("tbr"),
                    "acodec": best_audio.get("acodec"),
                    "protocol": best_audio.get("protocol"),
                } if best_audio else None,
                "diagnosticArgs": extractor_args,
            }

    started = time.perf_counter()
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(extract_probe),
            timeout=min(45, DOWNLOAD_PROVIDER_TIMEOUT_SECONDS),
        )
        return {
            "status": "ok",
            "videoId": test_video_id,
            "message": "yt-dlp metadata extraction succeeded; this host IP was accepted by YouTube for the diagnostic video.",
            "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
            "ytDlp": check_ytdlp(),
            "ffmpeg": check_ffmpeg(),
            "probe": info,
        }
    except Exception as exc:
        classified = classify_download_error(exc)
        return JSONResponse(
            status_code=classified.status_code,
            content={
                "status": "failed",
                "videoId": test_video_id,
                "message": "yt-dlp metadata extraction failed; inspect error.category to see whether this looks like IP blocking, bot-checking, rate limiting, or another extractor issue.",
                "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
                "ytDlp": check_ytdlp(),
                "ffmpeg": check_ffmpeg(),
                "error": classified.to_dict(),
            },
        )


@app.get("/health/download")
async def health_download():
    async with download_jobs_lock:
        queued = sum(1 for job in download_jobs.values() if job["status"] == "queued")
        running = sum(1 for job in download_jobs.values() if job["status"] == "running")
        completed = sum(1 for job in download_jobs.values() if job["status"] == "completed")
        failed = sum(1 for job in download_jobs.values() if job["status"] == "failed")

    youtubei_health = await _check_youtubei_health()

    return {
        "status": "ok",
        "queueSize": download_queue.qsize(),
        "workers": max(1, DOWNLOAD_WORKER_COUNT),
        "jobs": {
            "queued": queued,
            "running": running,
            "completed": completed,
            "failed": failed,
        },
        "extractor": {
            "youtubei": youtubei_health,
        },
        "metrics": download_metrics,
    }


@app.get("/metrics/download")
async def download_metrics_endpoint():
    async with download_jobs_lock:
        return {
            "queueSize": download_queue.qsize(),
            "workerCount": max(1, DOWNLOAD_WORKER_COUNT),
            "metrics": download_metrics,
        }


@app.post("/webhook/deploy")
async def github_webhook(request: Request):
    """
    GitHub webhook endpoint for auto-deployment.
    Set this as webhook URL in GitHub repo settings.
    """
    if not ENABLE_DEPLOY_WEBHOOK:
        raise HTTPException(404, "Deployment webhook is disabled")

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
async def download(data: DownloadIn, request: Request):
    video_id = validate_video_id(data.videoId)
    quality = validate_quality(data.quality if hasattr(data, "quality") else 2)

    await _enforce_download_rate_limit(_client_ip(request))

    job = await enqueue_download_job(video_id, quality)
    download_url = f"/download/file/{job['jobId']}"

    status_code = 200 if job["status"] == "completed" else 202
    return JSONResponse(
        status_code=status_code,
        content={
            "jobId": job["jobId"],
            "videoId": job["videoId"],
            "quality": job["quality"],
            "status": job["status"],
            "attempt": job["attempt"],
            "downloadUrl": download_url,
            "statusUrl": f"/download/jobs/{job['jobId']}",
            "error": job.get("error"),
        },
    )


@app.get("/download/jobs/{job_id}")
async def download_job_status(job_id: str):
    async with download_jobs_lock:
        job = download_jobs.get(job_id)

    if not job:
        raise HTTPException(404, "Download job not found")

    payload = {
        "jobId": job["jobId"],
        "videoId": job["videoId"],
        "quality": job["quality"],
        "status": job["status"],
        "attempt": job["attempt"],
        "createdAt": job["createdAt"],
        "updatedAt": job["updatedAt"],
        "providersTried": job.get("providersTried", []),
        "lastError": job.get("lastError"),
        "nextRetryInSeconds": job.get("nextRetryInSeconds", 0),
        "error": job.get("error"),
    }

    if job["status"] == "completed":
        payload["downloadUrl"] = f"/download/file/{job_id}"

    return payload


@app.get("/download/file/{job_id}")
async def download_file(job_id: str):
    async with download_jobs_lock:
        job = download_jobs.get(job_id)

    if not job:
        raise HTTPException(404, "Download job not found")

    if job["status"] != "completed":
        return JSONResponse(
            status_code=409,
            content={
                "jobId": job_id,
                "status": job["status"],
                "error": job.get("error"),
                "message": "Download is not ready yet",
            },
        )

    cache_path = job.get("cachePath") or os.path.join(CACHE_DIR, f"{job['videoId']}.mp3")
    if not os.path.exists(cache_path):
        raise HTTPException(410, "Downloaded file expired or unavailable")

    return build_file_response(cache_path, job.get("filename") or f"{job['videoId']}.mp3", "audio/mpeg")


@app.post("/download/direct")
async def download_direct(data: DownloadIn, request: Request, sync: bool = False):
    """
    Direct download using multiple fallback methods.
    Optimized for both localhost and Render deployment.

    Parameters:
    - videoId: YouTube video ID (required)
    - quality: 1=low (96kbps), 2=medium (128kbps), 3=high (320kbps)

    Output format is always MP3.
    """
    if not sync:
        return await download(data, request)

    await _enforce_download_rate_limit(_client_ip(request))

    data.videoId = validate_video_id(data.videoId)
    quality = validate_quality(data.quality if hasattr(data, "quality") else 2)

    job = await enqueue_download_job(data.videoId, quality)
    deadline = time.monotonic() + max(5, DOWNLOAD_SYNC_WAIT_SECONDS)

    while time.monotonic() < deadline:
        async with download_jobs_lock:
            current_job = download_jobs.get(job["jobId"])

        if not current_job:
            break
        if current_job["status"] == "completed":
            return await download_file(current_job["jobId"])
        if current_job["status"] == "failed":
            return JSONResponse(
                status_code=503,
                content={
                    "jobId": current_job["jobId"],
                    "status": current_job["status"],
                    "error": current_job.get("error"),
                    "message": "Download failed after retries",
                },
            )

        await asyncio.sleep(0.5)

    return JSONResponse(
        status_code=202,
        content={
            "jobId": job["jobId"],
            "status": "queued",
            "statusUrl": f"/download/jobs/{job['jobId']}",
            "downloadUrl": f"/download/file/{job['jobId']}",
            "message": "Download still processing",
        },
    )


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
            "cached_at": datetime.now().isoformat(),
            "path": os.path.join(CACHE_DIR, f"{video_id}.{format_ext}"),
            "size": os.path.getsize(os.path.join(CACHE_DIR, f"{video_id}.{format_ext}"))
            if os.path.exists(os.path.join(CACHE_DIR, f"{video_id}.{format_ext}")) else None,
        }

        with open(CACHE_MANIFEST, "w") as f:
            json.dump(manifest, f)
    except Exception as e:
        print(f"Manifest error: {e}")


async def stream_file(file_path, filename, tmpdir):
    """Stream file and cleanup"""
    def cleanup_tmpdir():
        try:
            os.remove(file_path)
        except Exception:
            pass
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass

    return build_file_response(
        file_path,
        filename,
        "audio/mpeg",
        cleanup=cleanup_tmpdir,
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
        return playlist_id if PLAYLIST_ID_RE.match(playlist_id) else ""
    elif 'spotify:playlist:' in link:
        # Extract from URI like: spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
        playlist_id = link.split(':')[-1]
        return playlist_id if PLAYLIST_ID_RE.match(playlist_id) else ""
    else:
        # Assume it's already just the ID
        value = link.strip()
        return value if PLAYLIST_ID_RE.match(value) else ""


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
        playlist_id = query_params["list"][0]
        return playlist_id if PLAYLIST_ID_RE.match(playlist_id) else ""

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
    video_ids = [validate_video_id(video_id) for video_id in video_ids]

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
                        ydl_opts = get_yt_dlp_options(tmpdir_info, BIN_DIR, format_ext, quality)
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

                        # Get optimized yt-dlp options
                        ydl_opts = get_yt_dlp_options(tmpdir, BIN_DIR, format_ext, quality)

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

        def cleanup_playlist_tmpdir():
            try:
                shutil.rmtree(playlist_tmpdir)
            except Exception:
                pass

        return build_file_response(
            zip_path,
            "playlist.zip",
            "application/zip",
            cleanup=cleanup_playlist_tmpdir,
            include_accept_ranges=False,
        )

    except HTTPException:
        raise
    except Exception as e:
        try:
            shutil.rmtree(playlist_tmpdir)
        except Exception:
            pass
        raise HTTPException(500, f"Playlist download failed: {str(e)}")

@app.get("/debug/cookies")
def debug_cookies():
    return {
        "writable_exists": os.path.exists(WRITABLE_COOKIES),
        "writable_path": WRITABLE_COOKIES,
        "writable_size": os.path.getsize(WRITABLE_COOKIES) if os.path.exists(WRITABLE_COOKIES) else 0,
        "sources": {
            s: {"exists": os.path.exists(s), "size": os.path.getsize(s) if os.path.exists(s) else 0}
            for s in COOKIES_SOURCES
        }
    }
