from flask import Flask, request, jsonify
import yt_dlp
import time
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError

app = Flask(__name__)

# In-memory caching for extracted data (Video ID -> (timestamp, payload)).
# This is for demonstration. For production, use Redis or external cache.
CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4  # 4 hours

# Rate-control: avoid making many concurrent back-to-back yt-dlp requests.
RATE_LIMIT_SECONDS = 1
LAST_REQUEST_TIME = 0

YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/v/|/embed/)([A-Za-z0-9_-]{11})")


def _has_valid_cookies_file(path="cookies.txt") -> bool:
    try:
        if not os.path.isfile(path):
            return False
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline().strip()
            return first_line.startswith("# Netscape")
    except Exception:
        return False


def _build_ydl_opts(use_cookies=True, format_spec="bestvideo+bestaudio/best"):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "socket_timeout": 20,
        "age_limit": 99,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "format": format_spec,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9"
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android"]
            }
        },
    }

    if use_cookies and _has_valid_cookies_file("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"

    return ydl_opts


def extract_video_id(url: str) -> str | None:
    if not url:
        return None

    m = YOUTUBE_ID_RE.search(url)
    if m:
        return m.group(1)

    return None


@app.route("/")
def home():
    return jsonify({
        "service": "youtube extractor",
        "status": "running",
        "mode": "youtube-only"
    })


@app.route("/extract", methods=["GET"])
def extract():
    url = request.args.get("url")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    # Expire old cache entries
    now = time.time()
    cached = CACHE.get(video_id)
    if cached:
        created_at, payload = cached
        if now - created_at < CACHE_TTL_SECONDS:
            # Return cached response directly.
            return jsonify(payload)
        else:
            del CACHE[video_id]


    try:
        # Rate control: ensure a small delay between yt-dlp requests.
        global LAST_REQUEST_TIME
        now = time.time()
        elapsed = now - LAST_REQUEST_TIME
        if elapsed < RATE_LIMIT_SECONDS:
            time.sleep(RATE_LIMIT_SECONDS - elapsed)
        LAST_REQUEST_TIME = time.time()

        ydl_opts = _build_ydl_opts(use_cookies=True)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            error_text = str(e)
            if "does not look like a Netscape format cookies file" in error_text:
                ydl_opts = _build_ydl_opts(use_cookies=False)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            elif "Requested format is not available" in error_text:
                # If requested format comes back invalid, try a less strict format.
                ydl_opts = _build_ydl_opts(use_cookies=True, format_spec="best")
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                except yt_dlp.utils.DownloadError:
                    ydl_opts = _build_ydl_opts(use_cookies=False, format_spec="best")
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
            else:
                raise

        formats = info.get("formats", [])

        progressive_url = None
        video_only_url = None
        audio_only_url = None

        best_video_height = -1
        best_audio_bitrate = -1

        for f in formats:

            stream_url = f.get("url")
            if not stream_url:
                continue

            ext = f.get("ext")
            vcodec = f.get("vcodec")
            acodec = f.get("acodec")
            height = f.get("height") or 0
            abr = f.get("abr") or 0

            # Skip HLS playlists
            if "m3u8" in stream_url:
                continue

            # Progressive stream (video + audio)
            # Accept any container (mp4/webm/etc) as long as it contains both video and audio.
            if vcodec != "none" and acodec != "none":
                progressive_url = stream_url
                break

            # Video-only stream
            if vcodec != "none" and acodec == "none":
                if height > best_video_height:
                    best_video_height = height
                    video_only_url = stream_url

            # Audio-only stream
            if acodec != "none" and vcodec == "none":
                if abr > best_audio_bitrate:
                    best_audio_bitrate = abr
                    audio_only_url = stream_url

        result = {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail")
        }

        if progressive_url:
            result["type"] = "progressive"
            result["url"] = progressive_url

        elif video_only_url and audio_only_url:
            result["type"] = "adaptive"
            result["video_url"] = video_only_url
            result["audio_url"] = audio_only_url

        elif video_only_url:
            result["type"] = "video_only"
            result["video_url"] = video_only_url

        elif audio_only_url:
            result["type"] = "audio_only"
            result["audio_url"] = audio_only_url

        else:
            # No usable streams were found; return a sample of the available formats
            # so the caller can diagnose what is available.
            format_info = [
                {
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                }
                for f in formats  # Show all formats for debugging
            ]
            return (
                jsonify({
                    "error": "No playable streams found",
                    "total_formats": len(formats),
                    "available_formats": format_info,
                }),
                500,
            )

        return jsonify(result)

    except yt_dlp.utils.DownloadError as e:
        detail_text = str(e)
        if "Sign in to confirm you\u2019re not a bot" in detail_text or "Sign in to confirm you\u2019re not a bot" in detail_text.replace("\u2019", "'"):
            return jsonify({
                "error": "Authentication required",
                "detail": (
                    "YouTube requires login cookies for this video. "
                    "Use a valid Netscape-format cookies file (cookies.txt), "
                    "or pass cookies using yt-dlp options like --cookies-from-browser. "
                    "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
                )
            }), 403

        return jsonify({
            "error": "Extraction failed",
            "detail": detail_text
        }), 500

    except Exception as e:
        return jsonify({
            "error": "Extraction failed",
            "detail": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
