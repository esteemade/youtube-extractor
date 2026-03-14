from flask import Flask, request, jsonify
import yt_dlp
import time
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError

app = Flask(__name__)

# Thread pool (shared for all requests)
executor = ThreadPoolExecutor(max_workers=2)

# Cache (video_id -> (timestamp, payload))
CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4  # 4 hours

# Rate limiting
RATE_LIMIT_SECONDS = 1
LAST_REQUEST_TIME = 0

# YouTube ID detection
YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")


def extract_video_id(url):
    if not url:
        return None
    match = YOUTUBE_ID_RE.search(url)
    if match:
        return match.group(1)
    return None


def _has_valid_cookies_file(path="cookies.txt"):
    try:
        if not os.path.isfile(path):
            return False
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline().strip()
        return first_line.startswith("# Netscape")
    except Exception:
        return False


def _build_ydl_opts(use_cookies=True):
    """
    Optimized options to prevent 'Requested format is not available' 
    and bypass bot detection.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": False,  # Changed to False to see useful debug info in Render logs
        "noplaylist": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "socket_timeout": 20,
        "retries": 5,
        "fragment_retries": 5,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        
        # This prevents the "Requested format is not available" error by allowing 
        # yt-dlp to fall back to the best available single file if needed.
        "format": "best/bestvideo+bestaudio",

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },

        "extractor_args": {
            "youtube": {
                # Including 'web' and 'ios' is critical because 'android' 
                # doesn't always support every video format/bitrate.
                "player_client": ["android", "web", "ios"],
                "skip": ["dash", "hls"] 
            }
        }
    }

    if use_cookies and _has_valid_cookies_file("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"

    return ydl_opts


def run_extraction(url, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


@app.route("/")
def home():
    return jsonify({
        "service": "youtube extractor",
        "status": "running",
        "mode": "hybrid-client"
    })


@app.route("/extract", methods=["GET"])
def extract():
    global LAST_REQUEST_TIME

    url = request.args.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    # Cache check
    now = time.time()
    cached = CACHE.get(video_id)
    if cached:
        created_at, payload = cached
        if now - created_at < CACHE_TTL_SECONDS:
            return jsonify(payload)
        else:
            del CACHE[video_id]

    try:
        # Rate limiting
        elapsed = now - LAST_REQUEST_TIME
        if elapsed < RATE_LIMIT_SECONDS:
            time.sleep(RATE_LIMIT_SECONDS - elapsed)
        LAST_REQUEST_TIME = time.time()

        ydl_opts = _build_ydl_opts(use_cookies=True)
        future = executor.submit(run_extraction, url, ydl_opts)

        try:
            info = future.result(timeout=25)
        except TimeoutError:
            future.cancel()
            return jsonify({
                "error": "Extraction timeout",
                "detail": "YouTube took too long to respond"
            }), 504

        formats = info.get("formats", [])
        
        # Extraction logic
        progressive_url = None
        video_only_url = None
        audio_only_url = None
        best_video_height = -1
        best_audio_bitrate = -1

        for f in formats:
            stream_url = f.get("url")
            if not stream_url:
                continue

            # Skip manifest files that aren't direct video links
            if "m3u8" in stream_url and "googlevideo" not in stream_url:
                continue

            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height") or 0
            abr = f.get("abr") or 0

            # Progressive (Video + Audio in one)
            if vcodec != "none" and acodec != "none":
                # We'll take the first stable progressive link found
                if not progressive_url:
                    progressive_url = stream_url

            # Video only
            elif vcodec != "none" and acodec == "none":
                if height >= best_video_height:
                    best_video_height = height
                    video_only_url = stream_url

            # Audio only
            elif acodec != "none" and vcodec == "none":
                if abr >= best_audio_bitrate:
                    best_audio_bitrate = abr
                    audio_only_url = stream_url

        result = {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "id": video_id
        }

        # Structure the response based on what was found
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
            return jsonify({"error": "No playable streams found"}), 500

        CACHE[video_id] = (time.time(), result)
        return jsonify(result)

    except yt_dlp.utils.DownloadError as e:
        detail_text = str(e)
        if "Sign in to confirm you're not a bot" in detail_text:
            return jsonify({
                "error": "Authentication required",
                "detail": "YouTube detected a bot. Update cookies.txt"
            }), 403
        return jsonify({
            "error": "Extraction failed",
            "detail": detail_text
        }), 500

    except Exception as e:
        return jsonify({
            "error": "Server error",
            "detail": str(e)
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
