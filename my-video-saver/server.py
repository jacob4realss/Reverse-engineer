#!/usr/bin/env python3
"""My Video Saver: a small personal TikTok video saver.

Serves one page (index.html) and two API endpoints:

  GET /api/info?url=TIKTOK_URL
      Looks the video up with yt-dlp and returns its title, creator,
      length, thumbnail and which qualities are available.

  GET /api/download?url=TIKTOK_URL&quality=best|compatible|audio
      Fetches the chosen file from TikTok through this server and
      streams it to the browser as a download.

Only TikTok links are accepted, requests are rate limited per visitor,
and an optional password (APP_PASSWORD) can restrict use to you.

Settings (environment variables):
  HOST              address to listen on (default 127.0.0.1)
  PORT              port to listen on (default 8000)
  APP_PASSWORD      if set, every API call must include ?key=APP_PASSWORD
  RATE_LIMIT        API requests allowed per visitor per minute (default 10)
"""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yt_dlp
from yt_dlp.networking import Request as YtRequest

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "10"))
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024  # refuse anything larger than 300 MB
CHUNK_SIZE = 64 * 1024

INDEX_HTML = Path(__file__).with_name("index.html")

# Only these hosts are accepted, so the server can't be used to fetch
# arbitrary websites on someone else's behalf.
ALLOWED_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}

QUALITIES = {"best", "compatible", "audio"}


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- rate limit

_hits: dict[str, deque[float]] = defaultdict(deque)
_hits_lock = threading.Lock()


def check_rate_limit(client: str) -> None:
    now = time.monotonic()
    with _hits_lock:
        window = _hits[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= RATE_LIMIT:
            raise ApiError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Too many requests. Please wait a minute and try again.",
            )
        window.append(now)


# ------------------------------------------------------------------- helpers


def validate_tiktok_url(raw: str | None) -> str:
    if not raw:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Paste a TikTok link first.")
    raw = raw.strip()
    if len(raw) > 500:
        raise ApiError(HTTPStatus.BAD_REQUEST, "That link is too long.")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in ALLOWED_HOSTS:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Only TikTok links are supported.")
    return raw


def is_watermarked(fmt: dict) -> bool:
    note = (fmt.get("format_note") or "").lower()
    return fmt.get("format_id") == "download" or "watermark" in note


def is_video(fmt: dict) -> bool:
    return (fmt.get("vcodec") or "none") != "none" and bool(fmt.get("url"))


def is_h264(fmt: dict) -> bool:
    codec = (fmt.get("vcodec") or "").lower()
    return codec.startswith(("h264", "avc"))


def size_of(fmt: dict) -> int:
    return fmt.get("filesize") or fmt.get("filesize_approx") or 0


def pick_format(info: dict, quality: str) -> dict | None:
    formats = [f for f in info.get("formats") or [] if not is_watermarked(f)]
    if quality == "audio":
        audio = [
            f for f in formats
            if (f.get("vcodec") or "none") == "none" and f.get("url")
        ]
        return audio[-1] if audio else None
    videos = [f for f in formats if is_video(f)]
    if quality == "compatible":
        videos = [f for f in videos if is_h264(f)]
    if not videos:
        return None
    return max(videos, key=lambda f: ((f.get("height") or 0), size_of(f)))


def describe(fmt: dict | None) -> dict | None:
    if fmt is None:
        return None
    return {
        "height": fmt.get("height"),
        "width": fmt.get("width"),
        "codec": fmt.get("vcodec"),
        "ext": fmt.get("ext"),
        "size_bytes": size_of(fmt) or None,
    }


def new_downloader() -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL(
        {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 20,
        }
    )


def look_up(ydl: yt_dlp.YoutubeDL, url: str) -> dict:
    try:
        info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as error:
        message = re.sub(r"^ERROR:\s*", "", str(error))
        raise ApiError(
            HTTPStatus.BAD_GATEWAY,
            f"Couldn't read that video from TikTok: {message[:200]}",
        ) from error
    if not info or info.get("_type") == "playlist":
        raise ApiError(HTTPStatus.BAD_REQUEST, "That link isn't a single video.")
    return info


def safe_filename(video_id: str, quality: str, ext: str) -> str:
    clean_id = re.sub(r"[^A-Za-z0-9_-]", "", str(video_id))[:40] or "video"
    clean_ext = re.sub(r"[^A-Za-z0-9]", "", ext or "mp4")[:5] or "mp4"
    return f"video_{clean_id}_{quality}.{clean_ext}"


# ------------------------------------------------------------------- handler


class Handler(BaseHTTPRequestHandler):
    server_version = "MyVideoSaver/1.0"

    def log_message(self, fmt: str, *args) -> None:  # keep logs short
        print(f"{self.address_string()} {fmt % args}", flush=True)

    # Security headers for every response.
    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        try:
            if parts.path in ("/", "/index.html"):
                self.serve_index()
            elif parts.path == "/api/config":
                self.send_json(HTTPStatus.OK, {"password_required": bool(APP_PASSWORD)})
            elif parts.path == "/api/info":
                self.guard(query)
                self.api_info(query)
            elif parts.path == "/api/download":
                self.guard(query)
                self.api_download(query)
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "Not found.")
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass  # the visitor closed the page mid-download
        except Exception as error:  # noqa: BLE001 - last-resort safety net
            print(f"Unexpected error: {error!r}", flush=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Something went wrong."})

    def guard(self, query: dict) -> None:
        if APP_PASSWORD:
            given = (query.get("key") or [""])[0]
            if not hmac.compare_digest(given.encode(), APP_PASSWORD.encode()):
                raise ApiError(HTTPStatus.UNAUTHORIZED, "Wrong or missing password.")
        check_rate_limit(self.client_address[0])

    def serve_index(self) -> None:
        body = INDEX_HTML.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def api_info(self, query: dict) -> None:
        url = validate_tiktok_url((query.get("url") or [None])[0])
        with new_downloader() as ydl:
            info = look_up(ydl, url)
        self.send_json(
            HTTPStatus.OK,
            {
                "id": info.get("id"),
                "title": info.get("title") or info.get("description") or "",
                "creator": info.get("uploader") or info.get("channel"),
                "creator_name": info.get("channel"),
                "duration_seconds": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "stats": {
                    "views": info.get("view_count"),
                    "likes": info.get("like_count"),
                    "comments": info.get("comment_count"),
                },
                "qualities": {q: describe(pick_format(info, q)) for q in sorted(QUALITIES)},
            },
        )

    def api_download(self, query: dict) -> None:
        url = validate_tiktok_url((query.get("url") or [None])[0])
        quality = (query.get("quality") or ["best"])[0]
        if quality not in QUALITIES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Unknown quality.")

        # Look the video up again: TikTok's file addresses expire quickly
        # and only work with the cookies from this same lookup.
        with new_downloader() as ydl:
            info = look_up(ydl, url)
            fmt = pick_format(info, quality)
            if fmt is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "That quality isn't available for this video.")
            try:
                upstream = ydl.urlopen(YtRequest(fmt["url"], headers=fmt.get("http_headers") or {}))
            except yt_dlp.networking.exceptions.RequestError as error:
                raise ApiError(HTTPStatus.BAD_GATEWAY, "TikTok refused the file request.") from error

            with upstream:
                length = upstream.headers.get("Content-Length")
                if length and int(length) > MAX_DOWNLOAD_BYTES:
                    raise ApiError(HTTPStatus.BAD_GATEWAY, "That file is too large.")
                ext = fmt.get("ext") or ("mp3" if quality == "audio" else "mp4")
                filename = safe_filename(info.get("id"), quality, ext)
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type",
                    upstream.headers.get("Content-Type") or "application/octet-stream",
                )
                if length:
                    self.send_header("Content-Length", length)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                sent = 0
                while chunk := upstream.read(CHUNK_SIZE):
                    sent += len(chunk)
                    if sent > MAX_DOWNLOAD_BYTES:
                        break
                    self.wfile.write(chunk)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    lock_note = "password ON" if APP_PASSWORD else "no password"
    print(f"My Video Saver running at http://{HOST}:{PORT} ({lock_note}, {RATE_LIMIT} requests/min)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
