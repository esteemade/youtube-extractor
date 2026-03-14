from flask import Flask, request, jsonify
import yt_dlp
import time
import re
import os

app = Flask(__name__)

# Simple in-memory cache
CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4 

YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/v/|/embed/)([A-Za-z0-9_-]{11})")

def extract_video_id(url: str) -> str | None:
    if not url:
        return None
    m = YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None

@app.route("/")
def home():
    return jsonify({
        "service": "youtube extractor",
        "status": "running",
        "environment": "cloud-optimized"
    })

@app.route("/extract", methods=["GET"])
def extract():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    # Cache handling
    now = time.time()
    if video_id in CACHE:
        created_at, payload = CACHE[video_id]
        if now - created_at < CACHE_TTL_SECONDS:
            return jsonify(payload)

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "nocheckcertificate": True,
            "socket_timeout": 15,
            "age_limit": 99,
            "geo_bypass": True,
            
            # 1. ADD COOKIE SUPPORT
            # Render needs these to bypass the 'Sign in' bot check
            "cookiefile": "cookies.txt" if os.path.exists("cookies.txt") else None,

            # 2. UPDATED HEADERS
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },

            # 3. FIX FOR "Requested format is not available"
            # We must tell yt-dlp to try multiple clients. 
            # 'ios' and 'mweb' are currently the most reliable for cloud servers.
            "extractor_args": {
                "youtube": {
                    "player_client": ["ios", "mweb", "android", "web"],
                    "skip": ["dash", "hls"] # Only get direct streams
                }
            },
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats", [])
        progressive_url = None
        video_only_url = None
        audio_only_url = None
        best_video_height = -1
        best_audio_bitrate = -1

        for f in formats:
            stream_url = f.get("url")
            # Only accept direct links to Google's video servers
            if not stream_url or "googlevideo" not in stream_url:
                continue

            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height") or 0
            abr = f.get("abr") or 0

            # Progressive (Video + Audio)
            if vcodec != "none" and acodec != "none":
                progressive_url = stream_url
                # On cloud servers, take the first working progressive link found
                break

            # Adaptive Video
            if vcodec != "none" and acodec == "none":
                if height > best_video_height:
                    best_video_height = height
                    video_only_url = stream_url

            # Adaptive Audio
            if acodec != "none" and vcodec == "none":
                if abr > best_audio_bitrate:
                    best_audio_bitrate = abr
                    audio_only_url = stream_url

        result = {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "id": video_id
        }

        if progressive_url:
            result["type"] = "progressive"
            result["url"] = progressive_url
        elif video_only_url and audio_only_url:
            result["type"] = "adaptive"
            result["video_url"] = video_only_url
            result["audio_url"] = audio_only_url
        else:
            return jsonify({"error": "No playable streams found"}), 500

        CACHE[video_id] = (time.time(), result)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Extraction failed", "detail": str(e)}), 500

if __name__ == "__main__":
    # REQUIRED FOR RENDER: Bind to the PORT environment variable
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
