from flask import Flask, request, jsonify
import yt_dlp
import time
import re
import os

app = Flask(__name__)

CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4  # 4 hours

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
        "mode": "production-optimized"
    })

@app.route("/extract", methods=["GET"])
def extract():
    url = request.args.get("url")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    now = time.time()
    cached = CACHE.get(video_id)
    if cached:
        created_at, payload = cached
        if now - created_at < CACHE_TTL_SECONDS:
            return jsonify(payload)
        else:
            del CACHE[video_id]

    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "nocheckcertificate": True,
            "socket_timeout": 10, # Lowered for faster fail-over
            "age_limit": 99,
            "geo_bypass": True,
            
            # CRITICAL FOR RENDER: 
            # 1. Use a modern User-Agent
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            
            # 2. Add Cookie Support (Make sure cookies.txt is in your root folder)
            "cookiefile": "cookies.txt" if os.path.exists("cookies.txt") else None,

            # 3. Force specific clients that are less likely to be blocked
            "extractor_args": {
                "youtube": {
                    "player_client": ["ios", "android", "web"], # 'ios' is the most successful on cloud IPs
                    "skip": ["dash", "hls"] # Speeds up extraction
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
            if not stream_url or "googlevideo" not in stream_url:
                continue

            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height") or 0
            abr = f.get("abr") or 0

            # Progressive stream
            if vcodec != "none" and acodec != "none":
                progressive_url = stream_url
                # Don't break immediately; keep looking for the best quality if needed
                # or break if you just want any working link.
                break 

            # Video-only
            if vcodec != "none" and acodec == "none":
                if height > best_video_height:
                    best_video_height = height
                    video_only_url = stream_url

            # Audio-only
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
        else:
            return jsonify({"error": "No playable streams found"}), 500

        CACHE[video_id] = (time.time(), result)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Extraction failed", "detail": str(e)}), 500

if __name__ == "__main__":
    # Render uses the PORT environment variable
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
