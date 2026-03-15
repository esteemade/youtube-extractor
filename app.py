from flask import Flask, request, jsonify
import yt_dlp
import time
import os
import re
import logging
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory caching for extracted data
CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4  # 4 hours

# Rate-limiting
RATE_LIMIT_SECONDS = 3  # Increased to 3 seconds
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
            # Check if file has content beyond the header
            second_line = f.readline().strip()
            return first_line.startswith("# Netscape") and bool(second_line)
    except Exception as e:
        logger.error(f"Error reading cookies file: {e}")
        return False


def _build_ydl_opts(use_cookies=True, format_spec=None):
    """Build yt-dlp options with better error handling"""
    
    # More flexible format specification
    if format_spec is None:
        format_spec = 'best/bestvideo+bestaudio/best'
    
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
                'player_client': ['android', 'web', 'ios'],  # Try multiple clients
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

    # Try multiple extraction strategies
    strategies = [
        # Strategy 1: Try with cookies, best format
        {"use_cookies": True, "format": None},
        # Strategy 2: Try with cookies, specific mp4 format
        {"use_cookies": True, "format": "best[ext=mp4]"},
        # Strategy 3: Try with cookies, best audio+video
        {"use_cookies": True, "format": "bestvideo+bestaudio"},
        # Strategy 4: Try without cookies, best format
        {"use_cookies": False, "format": None},
        # Strategy 5: Try without cookies, specific mp4
        {"use_cookies": False, "format": "best[ext=mp4]"},
    ]

    last_error = None
    video_info = None

    for i, strategy in enumerate(strategies):
        try:
            logger.info(f"Trying strategy {i+1}: cookies={strategy['use_cookies']}, format={strategy['format']}")
            
            ydl_opts = _build_ydl_opts(
                use_cookies=strategy['use_cookies'], 
                format_spec=strategy['format']
            )
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info
                info = ydl.extract_info(url, download=False)
                
                if info:
                    video_info = info
                    logger.info(f"Strategy {i+1} succeeded")
                    break
                    
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Strategy {i+1} failed: {e}")
            continue

    if not video_info:
        error_msg = last_error or "All extraction strategies failed"
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

    # Process the extracted info
    result = process_video_info(video_info)
    
    if result:
        # Cache the result
        CACHE[video_id] = (time.time(), result)
        return jsonify(result)
    else:
        return jsonify({
            "error": "Could not extract playable streams",
            "detail": "No suitable video/audio streams found"
        }), 500


def process_video_info(info):
    """Process extracted video info and return standardized format"""
    if not info:
        return None

    # Basic info
    result = {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "video_id": info.get("id"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date")
    }

    # Try to get direct URL from formats
    formats = info.get("formats", [])
    
    # Log available formats for debugging
    logger.info(f"Found {len(formats)} formats")
    
    # Priority 1: Find a progressive stream (video+audio combined)
    for f in formats:
        url = f.get("url")
        if not url or "m3u8" in url:
            continue
            
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        
        # Check if it's a progressive stream
        if vcodec != "none" and acodec != "none":
            result["type"] = "progressive"
            result["url"] = url
            result["format"] = f.get("ext", "mp4")
            result["quality"] = f.get("height", "unknown")
            result["filesize"] = f.get("filesize", f.get("filesize_approx", 0))
            logger.info(f"Found progressive stream: {result['quality']}p")
            return result
    
    # Priority 2: Try to combine video and audio streams
    video_streams = []
    audio_streams = []
    
    for f in formats:
        url = f.get("url")
        if not url or "m3u8" in url:
            continue
            
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        
        if vcodec != "none" and acodec == "none":
            video_streams.append(f)
        elif acodec != "none" and vcodec == "none":
            audio_streams.append(f)
    
    if video_streams and audio_streams:
        # Sort video streams by quality (height) and get the best
        video_streams.sort(key=lambda x: x.get("height", 0), reverse=True)
        # Sort audio streams by bitrate and get the best
        audio_streams.sort(key=lambda x: x.get("abr", 0), reverse=True)
        
        best_video = video_streams[0]
        best_audio = audio_streams[0]
        
        result["type"] = "adaptive"
        result["video_url"] = best_video.get("url")
        result["audio_url"] = best_audio.get("url")
        result["video_format"] = best_video.get("ext")
        result["audio_format"] = best_audio.get("ext")
        result["video_quality"] = best_video.get("height", "unknown")
        result["audio_quality"] = best_audio.get("abr", "unknown")
        logger.info(f"Found adaptive streams: video={result['video_quality']}p, audio={result['audio_quality']}kbps")
        return result
    
    # Priority 3: Look for requested_formats (already combined by yt-dlp)
    requested_formats = info.get("requested_formats", [])
    if requested_formats:
        result["type"] = "adaptive"
        if len(requested_formats) >= 2:
            result["video_url"] = requested_formats[0].get("url")
            result["audio_url"] = requested_formats[1].get("url")
            result["video_format"] = requested_formats[0].get("ext")
            result["audio_format"] = requested_formats[1].get("ext")
            logger.info("Using requested_formats from yt-dlp")
            return result
    
    # Priority 4: Try the main URL if nothing else worked
    main_url = info.get("url")
    if main_url and "m3u8" not in main_url:
        result["type"] = "direct"
        result["url"] = main_url
        result["format"] = info.get("ext", "unknown")
        logger.info("Using direct URL from info")
        return result
    
    # If we get here, no playable streams were found
    logger.warning("No playable streams found in video info")
    return None


@app.route("/formats", methods=["GET"])
def list_formats():
    """Debug endpoint to list all available formats for a video"""
    url = request.args.get("url")
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    try:
        ydl_opts = _build_ydl_opts(use_cookies=True, format_spec=None)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = []
            for f in info.get("formats", []):
                formats.append({
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                    "height": f.get("height"),
                    "width": f.get("width"),
                    "filesize": f.get("filesize"),
                    "tbr": f.get("tbr"),
                    "url_preview": f.get("url")[:100] if f.get("url") else None
                })
            
            return jsonify({
                "video_id": info.get("id"),
                "title": info.get("title"),
                "format_count": len(formats),
                "formats": formats
            })
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint for Render"""
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
