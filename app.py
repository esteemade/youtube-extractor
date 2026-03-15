from flask import Flask, request, jsonify
import yt_dlp
import time
import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory caching for extracted data (Video ID -> (timestamp, payload))
CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4  # 4 hours

# Rate-limiting
RATE_LIMIT_SECONDS = 2  # Increased to 2 seconds
LAST_REQUEST_TIME = 0

YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/v/|/embed/)([A-Za-z0-9_-]{11})")


def _has_valid_cookies_file(path="cookies.txt") -> bool:
    """Check if cookies file exists and has correct format"""
    try:
        if not os.path.isfile(path):
            logger.warning(f"Cookies file not found at {path}")
            return False
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline().strip()
            # Also check if file has content beyond the header
            second_line = f.readline().strip()
            return first_line.startswith("# Netscape") and bool(second_line)
    except Exception as e:
        logger.error(f"Error reading cookies file: {e}")
        return False


def _build_ydl_opts(use_cookies=True, format_spec=None):
    """Build yt-dlp options with better error handling"""
    
    # Progressive format as default
    if format_spec is None:
        format_spec = 'best[ext=mp4]/best'  # Prefer mp4 format
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'socket_timeout': 30,
        'age_limit': 99,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        'format': format_spec,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],  # Try multiple clients
                'skip': ['hls', 'dash'],  # Skip some formats to speed up extraction
            }
        },
        # Add these options to help with bot detection
        'extract_flat': False,
        'force_generic_extractor': False,
    }

    # Add cookies if available
    cookies_path = os.environ.get('COOKIES_PATH', 'cookies.txt')
    if use_cookies and _has_valid_cookies_file(cookies_path):
        ydl_opts['cookiefile'] = cookies_path
        logger.info(f"Using cookies from {cookies_path}")
    else:
        logger.warning("No valid cookies file found, proceeding without cookies")
        
    return ydl_opts


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats"""
    if not url:
        return None

    # Handle youtu.be format
    m = YOUTUBE_ID_RE.search(url)
    if m:
        return m.group(1)
    
    # Handle direct video ID (11 characters)
    if re.match(r'^[A-Za-z0-9_-]{11}$', url):
        return url

    return None


@app.route("/")
def home():
    """Health check endpoint"""
    cookies_valid = _has_valid_cookies_file()
    return jsonify({
        "service": "youtube extractor",
        "status": "running",
        "cookies_configured": cookies_valid,
        "mode": "youtube-only"
    })


@app.route("/extract", methods=["GET"])
def extract():
    """Main endpoint to extract video information"""
    url = request.args.get("url")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    # Check cache
    now = time.time()
    cached = CACHE.get(video_id)
    if cached:
        created_at, payload = cached
        if now - created_at < CACHE_TTL_SECONDS:
            logger.info(f"Cache hit for video {video_id}")
            return jsonify(payload)
        else:
            del CACHE[video_id]

    # Rate limiting
    global LAST_REQUEST_TIME
    elapsed = now - LAST_REQUEST_TIME
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    LAST_REQUEST_TIME = time.time()

    # Try different format strategies
    format_strategies = [
        'best[ext=mp4]/best',  # Best mp4 format
        'best',  # Best quality regardless of container
        'bestvideo+bestaudio/best',  # Adaptive formats
    ]

    result = None
    last_error = None

    for format_spec in format_strategies:
        try:
            logger.info(f"Trying format strategy: {format_spec} for video {video_id}")
            
            # Try with cookies first
            ydl_opts = _build_ydl_opts(use_cookies=True, format_spec=format_spec)
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
            # Process the extracted info
            result = process_video_info(info)
            if result:
                logger.info(f"Successfully extracted video {video_id} with format {format_spec}")
                break
                
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Failed with format {format_spec}: {e}")
            
            # If cookies error, try without cookies
            if "cookies" in str(e).lower() or "sign in" in str(e).lower():
                try:
                    logger.info(f"Retrying without cookies for {video_id}")
                    ydl_opts = _build_ydl_opts(use_cookies=False, format_spec=format_spec)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    result = process_video_info(info)
                    if result:
                        break
                except Exception as e2:
                    logger.error(f"Also failed without cookies: {e2}")
                    continue

    if result:
        # Cache the result
        CACHE[video_id] = (time.time(), result)
        return jsonify(result)
    else:
        # Provide helpful error message
        error_msg = last_error or "Unknown error"
        logger.error(f"All extraction attempts failed for {video_id}: {error_msg}")
        
        if "Sign in" in error_msg or "bot" in error_msg:
            return jsonify({
                "error": "Authentication required",
                "detail": "YouTube is blocking this request. Please configure cookies.txt file.",
                "solution": "Export cookies from your browser as Netscape format and save as cookies.txt"
            }), 403
        else:
            return jsonify({
                "error": "Extraction failed",
                "detail": error_msg
            }), 500


def process_video_info(info):
    """Process extracted video info and return standardized format"""
    if not info:
        return None

    formats = info.get("formats", [])
    
    # Get the best available stream
    result = {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "video_id": info.get("id")
    }

    # Try to get direct URL from formats
    for f in formats:
        url = f.get("url")
        if not url:
            continue
            
        # Skip HLS playlists
        if "m3u8" in url:
            continue
            
        # Check if it's a progressive stream (has both video and audio)
        if f.get("vcodec") != "none" and f.get("acodec") != "none":
            result["type"] = "progressive"
            result["url"] = url
            result["format"] = f.get("ext", "mp4")
            return result
    
    # If no progressive stream found, try to combine video and audio
    video_streams = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none"]
    audio_streams = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"]
    
    if video_streams and audio_streams:
        # Get best video and audio
        best_video = max(video_streams, key=lambda x: x.get("height", 0))
        best_audio = max(audio_streams, key=lambda x: x.get("abr", 0))
        
        result["type"] = "adaptive"
        result["video_url"] = best_video.get("url")
        result["audio_url"] = best_audio.get("url")
        result["video_format"] = best_video.get("ext")
        result["audio_format"] = best_audio.get("ext")
        return result
    
    # If no streams found, try to get from requested_formats
    requested_formats = info.get("requested_formats", [])
    if requested_formats:
        result["type"] = "adaptive"
        result["video_url"] = requested_formats[0].get("url")
        result["audio_url"] = requested_formats[1].get("url") if len(requested_formats) > 1 else None
        return result
    
    # Last resort: try the main URL
    main_url = info.get("url")
    if main_url:
        result["type"] = "direct"
        result["url"] = main_url
        return result
    
    return None


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint for Render"""
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
