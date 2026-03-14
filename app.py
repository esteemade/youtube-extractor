from flask import Flask, request, jsonify
import yt_dlp
import time
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError

app = Flask(__name__)

# Increased workers so one or two "hangs" don't kill the whole app
executor = ThreadPoolExecutor(max_workers=5)

CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4 

RATE_LIMIT_SECONDS = 1
LAST_REQUEST_TIME = 0

YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")

def extract_video_id(url):
    if not url: return None
    match = YOUTUBE_ID_RE.search(url)
    return match.group(1) if match else None

def _has_valid_cookies_file(path="cookies.txt"):
    try:
        if not os.path.isfile(path): return False
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.readline().strip().startswith("# Netscape")
    except Exception: return False

def _build_ydl_opts(use_cookies=True):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "nocheckcertificate": True,
        
        # TIMEOUT PROTECTION: These are key to stop infinite loading
        "socket_timeout": 7,     # If no response in 7s, give up
        "retries": 1,            # Don't keep trying if blocked
        "extract_flat": False,
        
        "format": "best/bestvideo+bestaudio",

        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },

        "extractor_args": {
            "youtube": {
                # 'ios' is currently the most stable for server-side extraction
                "player_client": ["ios", "web"],
                "skip": ["dash", "hls"]
            }
        }
    }

    if use_cookies and _has_valid_cookies_file("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"

    return ydl_opts

def run_extraction(url, ydl_opts):
    # This runs inside the thread pool
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

@app.route("/")
def home():
    return jsonify({"service": "youtube-extractor", "status": "online"})

@app.route("/extract", methods=["GET"])
def extract():
    global LAST_REQUEST_TIME
    url = request.args.get("url")
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid URL"}), 400

    # Cache Check
    now = time.time()
    if video_id in CACHE:
        ts, payload = CACHE[video_id]
        if now - ts < CACHE_TTL_SECONDS:
            return jsonify(payload)

    # Rate Limiting
    elapsed = now - LAST_REQUEST_TIME
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    LAST_REQUEST_TIME = time.time()

    ydl_opts = _build_ydl_opts(use_cookies=True)
    
    # Execute with a strict timeout
    future = executor.submit(run_extraction, url, ydl_opts)
    try:
        # We wait 15 seconds. If yt-dlp hasn't finished, we kill the request.
        info = future.result(timeout=15)
    except TimeoutError:
        return jsonify({"error": "Timeout", "detail": "YouTube is taking too long or blocking the request"}), 504
    except Exception as e:
        return jsonify({"error": "Extraction failed", "detail": str(e)}), 500

    # Process Formats
    formats = info.get("formats", [])
    res_data = {"title": info.get("title"), "thumbnail": info.get("thumbnail"), "duration": info.get("duration")}
    
    # Simple selection logic
    best_audio = None
    best_video = None
    
    for f in formats:
        f_url = f.get("url")
        if not f_url or "googlevideo" not in f_url: continue
        
        if f.get("vcodec") != "none" and f.get("acodec") != "none":
            res_data.update({"type": "progressive", "url": f_url})
            break # Found a combined stream, good enough
        
        if f.get("acodec") != "none" and f.get("vcodec") == "none":
            best_audio = f_url
        if f.get("vcodec") != "none" and f.get("acodec") == "none":
            best_video = f_url

    if "url" not in res_data:
        if best_video and best_audio:
            res_data.update({"type": "adaptive", "video_url": best_video, "audio_url": best_audio})
        elif best_video:
            res_data.update({"type": "video_only", "video_url": best_video})
        elif best_audio:
            res_data.update({"type": "audio_only", "audio_url": best_audio})
        else:
            return jsonify({"error": "No playable streams found"}), 500

    CACHE[video_id] = (time.time(), res_data)
    return jsonify(res_data)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
