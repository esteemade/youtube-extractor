from flask import Flask, request, jsonify
import yt_dlp
import time
import os
import re
import logging
import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory caching for extracted data
CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4  # 4 hours

# Rate-limiting
RATE_LIMIT_SECONDS = 3
LAST_REQUEST_TIME = 0

YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/v/|/embed/)([A-Za-z0-9_-]{11})")


def setup_cookies():
    """Setup cookies from file or environment variable"""
    cookies_path = os.environ.get('COOKIES_PATH', 'cookies.txt')
    
    # Check if cookies exist in environment variable
    cookies_b64 = os.environ.get('COOKIES_B64')
    if cookies_b64 and not os.path.exists(cookies_path):
        try:
            cookies_content = base64.b64decode(cookies_b64).decode('utf-8')
            with open(cookies_path, 'w') as f:
                f.write(cookies_content)
            logger.info("Cookies extracted from environment variable")
        except Exception as e:
            logger.error(f"Failed to extract cookies from env: {e}")
    
    return cookies_path


def _has_valid_cookies_file(path="cookies.txt") -> bool:
    """Check if cookies file exists and has correct format"""
    try:
        if not os.path.isfile(path):
            logger.warning(f"Cookies file not found at {path}")
            return False
        
        # Check file size
        file_size = os.path.getsize(path)
        if file_size < 100:  # Cookies file should be at least 100 bytes
            logger.warning(f"Cookies file too small ({file_size} bytes)")
            return False
            
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            
            # Check for Netscape format
            if not content.startswith("# Netscape"):
                logger.warning("Cookies file not in Netscape format")
                return False
            
            # Check for actual cookie entries (lines not starting with #)
            cookie_lines = [line for line in content.split('\n') 
                          if line.strip() and not line.startswith('#')]
            
            if len(cookie_lines) < 5:  # Should have multiple cookies
                logger.warning(f"Only {len(cookie_lines)} cookie entries found")
                return False
                
            logger.info(f"Valid cookies file found with {len(cookie_lines)} entries")
            return True
            
    except Exception as e:
        logger.error(f"Error reading cookies file: {e}")
        return False


def _build_ydl_opts(use_cookies=True, format_spec=None, extractor_args=None):
    """Build yt-dlp options with better error handling"""
    
    if format_spec is None:
        format_spec = 'best[height<=1080][ext=mp4]/best[height<=1080]/best'
    
    # Base options
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
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'Connection': 'keep-alive',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web', 'ios', 'tv'],  # Try all clients
                'skip': ['hls', 'dash'],  # Skip some formats to speed up extraction
            }
        },
        # Add these to help with bot detection
        'extract_flat': False,
        'force_generic_extractor': False,
        'youtube_include_dash_manifest': False,  # Reduce load
        'extractor_retries': 3,  # Retry on failure
        'file_access_retries': 3,
    }

    # Add cookies if available
    cookies_path = setup_cookies()
    if use_cookies and _has_valid_cookies_file(cookies_path):
        ydl_opts['cookiefile'] = cookies_path
        logger.info(f"Using cookies from {cookies_path}")
    else:
        logger.warning("No valid cookies file found, proceeding without cookies")
        # Try to use browser cookies as fallback
        try:
            ydl_opts['cookiesfrombrowser'] = ('chrome',)  # Try to get from Chrome
            logger.info("Attempting to use browser cookies")
        except:
            pass
        
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
    cookies_path = setup_cookies()
    return jsonify({
        "service": "youtube extractor",
        "status": "running",
        "cookies_configured": cookies_valid,
        "cookies_path": cookies_path,
        "cookies_file_exists": os.path.exists(cookies_path),
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

    # Extensive extraction strategies
    strategies = [
        # Strategy 1: With cookies, best quality
        {"use_cookies": True, "format": 'best[height<=1080][ext=mp4]/best[height<=1080]/best', "client": ['android']},
        # Strategy 2: With cookies, web client
        {"use_cookies": True, "format": 'best', "client": ['web']},
        # Strategy 3: With cookies, tv client
        {"use_cookies": True, "format": 'best', "client": ['tv']},
        # Strategy 4: With cookies, ios client
        {"use_cookies": True, "format": 'best', "client": ['ios']},
        # Strategy 5: Without cookies, android
        {"use_cookies": False, "format": 'best', "client": ['android']},
        # Strategy 6: Without cookies, web
        {"use_cookies": False, "format": 'best', "client": ['web']},
    ]

    last_error = None
    video_info = None
    used_strategy = None

    for i, strategy in enumerate(strategies):
        try:
            logger.info(f"Trying strategy {i+1}: cookies={strategy['use_cookies']}, client={strategy['client']}")
            
            ydl_opts = _build_ydl_opts(
                use_cookies=strategy['use_cookies'], 
                format_spec=strategy['format']
            )
            
            # Override extractor args for this strategy
            ydl_opts['extractor_args']['youtube']['player_client'] = strategy['client']
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Try to extract info with timeout
                info = ydl.extract_info(url, download=False)
                
                if info and info.get('formats'):
                    video_info = info
                    used_strategy = i + 1
                    logger.info(f"Strategy {i+1} succeeded with {len(info.get('formats', []))} formats")
                    break
                    
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Strategy {i+1} failed: {str(e)[:200]}")
            continue

    if not video_info:
        error_msg = last_error or "All extraction strategies failed"
        logger.error(f"All extraction attempts failed for {video_id}")
        
        # Check if it's an authentication issue
        if any(term in error_msg.lower() for term in ['sign in', 'login', 'bot', 'cookie']):
            cookies_valid = _has_valid_cookies_file()
            return jsonify({
                "error": "Authentication required",
                "detail": f"YouTube is blocking this request. Cookies valid: {cookies_valid}",
                "solution": "Please ensure cookies.txt is properly configured and not expired. Try exporting fresh cookies from your browser while logged into YouTube.",
                "cookies_status": {
                    "file_exists": os.path.exists(setup_cookies()),
                    "valid_format": cookies_valid,
                    "error_hint": error_msg[:200]
                }
            }), 403
        else:
            return jsonify({
                "error": "Extraction failed",
                "detail": error_msg[:500]
            }), 500

    # Process the extracted info
    result = process_video_info(video_info)
    
    if result:
        # Add strategy info for debugging
        result['_debug'] = {
            'strategy_used': used_strategy,
            'formats_found': len(video_info.get('formats', [])),
            'extractor': video_info.get('extractor')
        }
        
        # Cache the result
        CACHE[video_id] = (time.time(), result)
        return jsonify(result)
    else:
        return jsonify({
            "error": "Could not extract playable streams",
            "detail": "No suitable video/audio streams found",
            "available_formats": len(video_info.get('formats', []))
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
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
    }

    # Get all formats
    formats = info.get("formats", [])
    
    if not formats:
        # Try to get from requested_formats
        requested = info.get("requested_formats", [])
        if requested:
            formats = requested
    
    logger.info(f"Processing {len(formats)} formats")
    
    # Priority 1: Find best progressive stream (video+audio combined)
    progressive_streams = []
    for f in formats:
        url = f.get("url")
        if not url or "m3u8" in url:
            continue
            
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        
        if vcodec != "none" and acodec != "none" and vcodec != "none":
            progressive_streams.append(f)
    
    if progressive_streams:
        # Sort by quality (height) and get the best
        progressive_streams.sort(key=lambda x: x.get("height", 0) or x.get("quality", 0), reverse=True)
        best = progressive_streams[0]
        
        result["type"] = "progressive"
        result["url"] = best.get("url")
        result["format"] = best.get("ext", "mp4")
        result["quality"] = best.get("height", "unknown")
        result["filesize"] = best.get("filesize", best.get("filesize_approx", 0))
        result["has_audio"] = True
        result["has_video"] = True
        logger.info(f"Found progressive stream: {result['quality']}p")
        return result
    
    # Priority 2: Try to find separate video and audio streams
    video_streams = []
    audio_streams = []
    
    for f in formats:
        url = f.get("url")
        if not url or "m3u8" in url:
            continue
            
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        
        # Video-only stream
        if vcodec != "none" and (acodec == "none" or not acodec):
            video_streams.append(f)
        # Audio-only stream
        elif (acodec != "none" or acodec) and (vcodec == "none" or not vcodec):
            audio_streams.append(f)
    
    if video_streams and audio_streams:
        # Sort video by quality
        video_streams.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
        # Sort audio by bitrate
        audio_streams.sort(key=lambda x: x.get("abr", 0) or 0, reverse=True)
        
        best_video = video_streams[0]
        best_audio = audio_streams[0]
        
        result["type"] = "adaptive"
        result["video_url"] = best_video.get("url")
        result["audio_url"] = best_audio.get("url")
        result["video_format"] = best_video.get("ext")
        result["audio_format"] = best_audio.get("ext")
        result["video_quality"] = best_video.get("height", "unknown")
        result["audio_quality"] = best_audio.get("abr", "unknown")
        result["video_filesize"] = best_video.get("filesize", best_video.get("filesize_approx", 0))
        result["audio_filesize"] = best_audio.get("filesize", best_audio.get("filesize_approx", 0))
        logger.info(f"Found adaptive streams: video={result['video_quality']}p, audio={result['audio_quality']}kbps")
        return result
    
    # Priority 3: Look for any stream with URL
    for f in formats:
        url = f.get("url")
        if url and "m3u8" not in url:
            result["type"] = "direct"
            result["url"] = url
            result["format"] = f.get("ext", "unknown")
            result["quality"] = f.get("height", f.get("quality", "unknown"))
            logger.info(f"Found direct stream")
            return result
    
    logger.warning("No playable streams found in video info")
    return None


@app.route("/debug/cookies", methods=["GET"])
def debug_cookies():
    """Debug endpoint to check cookies status"""
    cookies_path = setup_cookies()
    cookies_valid = _has_valid_cookies_file(cookies_path)
    
    status = {
        "cookies_path": cookies_path,
        "file_exists": os.path.exists(cookies_path),
        "valid_format": cookies_valid,
    }
    
    if os.path.exists(cookies_path):
        status["file_size"] = os.path.getsize(cookies_path)
        try:
            with open(cookies_path, 'r') as f:
                content = f.read()
                lines = content.split('\n')
                cookie_lines = [l for l in lines if l.strip() and not l.startswith('#')]
                status["total_cookies"] = len(cookie_lines)
                status["first_10_chars"] = content[:100]  # Show beginning of file
        except Exception as e:
            status["read_error"] = str(e)
    
    return jsonify(status)


@app.route("/debug/formats", methods=["GET"])
def debug_formats():
    """Debug endpoint to list all available formats for a video"""
    url = request.args.get("url")
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    try:
        ydl_opts = _build_ydl_opts(use_cookies=True, format_spec=None)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Extract all format info
            formats = []
            for f in info.get("formats", []):
                format_info = {
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                    "height": f.get("height"),
                    "width": f.get("width"),
                    "filesize": f.get("filesize"),
                    "tbr": f.get("tbr"),
                    "fps": f.get("fps"),
                    "quality": f.get("quality"),
                }
                
                # Add URL preview (truncated for safety)
                url = f.get("url")
                if url:
                    format_info["has_url"] = True
                    format_info["url_preview"] = url[:50] + "..." if len(url) > 50 else url
                
                formats.append(format_info)
            
            return jsonify({
                "video_id": info.get("id"),
                "title": info.get("title"),
                "format_count": len(formats),
                "extractor": info.get("extractor"),
                "formats": formats
            })
            
    except Exception as e:
        return jsonify({
            "error": str(e),
            "error_type": type(e).__name__
        }), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint for Render"""
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
