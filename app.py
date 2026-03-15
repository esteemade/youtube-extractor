from flask import Flask, request, jsonify
import yt_dlp
import time
import os
import re
import logging
import random
import subprocess
import sys

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory caching for extracted data
CACHE = {}
CACHE_TTL_SECONDS = 60 * 60 * 4  # 4 hours

# Rate-limiting
RATE_LIMIT_SECONDS = 5  # Increased to 5 seconds
LAST_REQUEST_TIME = 0

YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/v/|/embed/)([A-Za-z0-9_-]{11})")

# Rotating user agents
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_1_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1',
]


def clean_cookies_file(path="cookies.txt"):
    """Clean up duplicate entries in cookies file"""
    try:
        if not os.path.exists(path):
            return False
            
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Remove duplicates while preserving order
        seen = set()
        unique_lines = []
        
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith('#'):
                unique_lines.append(line)
                continue
            
            # Use the cookie name and domain as key for deduplication
            parts = line_stripped.split('\t')
            if len(parts) >= 5:
                key = f"{parts[0]}_{parts[4]}"
                if key not in seen:
                    seen.add(key)
                    unique_lines.append(line)
        
        # Write back unique lines
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(unique_lines)
        
        logger.info(f"Cleaned cookies file: {len(lines)} lines -> {len(unique_lines)} lines")
        return True
    except Exception as e:
        logger.error(f"Error cleaning cookies file: {e}")
        return False


def _has_valid_cookies_file(path="cookies.txt") -> bool:
    """Check if cookies file exists and has correct format"""
    try:
        # Clean the cookies file first
        clean_cookies_file(path)
        
        if not os.path.isfile(path):
            logger.warning(f"Cookies file not found at {path}")
            return False
        
        # Check file size
        file_size = os.path.getsize(path)
        if file_size < 100:
            logger.warning(f"Cookies file too small ({file_size} bytes)")
            return False
            
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            
            # Check for Netscape format
            if not content.strip().startswith("# Netscape"):
                logger.warning("Cookies file not in Netscape format")
                return False
            
            # Count actual cookie entries
            cookie_lines = []
            for line in content.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split('\t')
                    if len(parts) >= 7:  # Netscape format should have 7 fields
                        cookie_lines.append(line)
            
            if len(cookie_lines) < 5:
                logger.warning(f"Only {len(cookie_lines)} valid cookie entries found")
                return False
                
            logger.info(f"Valid cookies file found with {len(cookie_lines)} entries")
            return True
            
    except Exception as e:
        logger.error(f"Error reading cookies file: {e}")
        return False


def _build_ydl_opts(use_cookies=True, format_spec=None, client_type='android'):
    """Build yt-dlp options with better error handling"""
    
    if format_spec is None:
        format_spec = 'best[height<=1080][ext=mp4]/best[height<=1080]/best'
    
    # Rotate user agent
    user_agent = random.choice(USER_AGENTS)
    
    # Different client configurations
    client_configs = {
        'android': {
            'player_client': ['android', 'android_creator', 'android_embedded'],
            'extractor_args': {'youtube': {'player_client': ['android', 'android_creator']}}
        },
        'web': {
            'player_client': ['web', 'web_creator', 'web_embedded'],
            'extractor_args': {'youtube': {'player_client': ['web', 'web_creator']}}
        },
        'ios': {
            'player_client': ['ios', 'ios_creator', 'ios_embedded'],
            'extractor_args': {'youtube': {'player_client': ['ios', 'ios_creator']}}
        },
        'tv': {
            'player_client': ['tv', 'tv_embedded'],
            'extractor_args': {'youtube': {'player_client': ['tv', 'tv_embedded']}}
        }
    }
    
    client = client_configs.get(client_type, client_configs['android'])
    
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
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'Connection': 'keep-alive',
        },
        'extract_flat': False,
        'force_generic_extractor': False,
        'extractor_retries': 5,
        'file_access_retries': 5,
        'retry_sleep': 2,
    }
    
    # Add client-specific settings
    ydl_opts.update(client)
    
    # Add cookies if available
    if use_cookies and _has_valid_cookies_file('cookies.txt'):
        ydl_opts['cookiefile'] = 'cookies.txt'
        logger.info("Using cookies from cookies.txt")
    else:
        logger.warning("No valid cookies file found, proceeding without cookies")
        
    return ydl_opts


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats"""
    if not url:
        return None

    m = YOUTUBE_ID_RE.search(url)
    if m:
        return m.group(1)
    
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
        "cookies_file_exists": os.path.exists('cookies.txt'),
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

    # Comprehensive strategy combinations
    strategies = []
    
    # Format strategies
    formats = [
        'best[height<=1080][ext=mp4]/best[height<=1080]/best',
        'best[ext=mp4]/best',
        'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'best',
        'worst',
        '18',  # 360p mp4
        '22',  # 720p mp4
        '137+140',  # 1080p video + m4a audio
    ]
    
    # Client types
    clients = ['android', 'web', 'ios', 'tv']
    
    # Cookie options
    cookie_options = [True, False]
    
    # Build all strategy combinations
    for use_cookies in cookie_options:
        for client in clients:
            for fmt in formats[:3]:  # Limit to first 3 formats to avoid too many attempts
                strategies.append({
                    'use_cookies': use_cookies,
                    'client': client,
                    'format': fmt
                })

    last_error = None
    video_info = None
    used_strategy = None

    # Try each strategy
    for i, strategy in enumerate(strategies[:15]):  # Limit to first 15 strategies
        try:
            logger.info(f"Trying strategy {i+1}: cookies={strategy['use_cookies']}, client={strategy['client']}, format={strategy['format']}")
            
            # Add small random delay between attempts
            time.sleep(random.uniform(0.5, 1.5))
            
            ydl_opts = _build_ydl_opts(
                use_cookies=strategy['use_cookies'], 
                format_spec=strategy['format'],
                client_type=strategy['client']
            )
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                if info and info.get('formats'):
                    video_info = info
                    used_strategy = strategy
                    logger.info(f"Success with strategy {i+1}")
                    break
                    
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Strategy {i+1} failed: {str(e)[:100]}")
            continue

    if not video_info:
        error_msg = last_error or "All extraction strategies failed"
        logger.error(f"All extraction attempts failed for {video_id}")
        
        # Try one last approach with subprocess (sometimes works when yt-dlp lib fails)
        try:
            logger.info("Trying subprocess approach as last resort")
            result = subprocess.run(
                ['yt-dlp', '--get-url', '--format', 'best', url],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return jsonify({
                    "type": "direct",
                    "url": result.stdout.strip(),
                    "title": "Extracted via subprocess",
                    "note": "Used fallback extraction method"
                })
        except Exception as e:
            logger.error(f"Subprocess approach failed: {e}")
        
        cookies_valid = _has_valid_cookies_file()
        return jsonify({
            "error": "Extraction failed",
            "detail": error_msg[:500],
            "cookies_status": {
                "file_exists": os.path.exists('cookies.txt'),
                "valid_format": cookies_valid,
            }
        }), 500

    # Process the extracted info
    result = process_video_info(video_info)
    
    if result:
        result['_debug'] = {
            'strategy_used': used_strategy,
            'formats_found': len(video_info.get('formats', [])),
        }
        
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

    result = {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "video_id": info.get("id"),
        "uploader": info.get("uploader"),
    }

    formats = info.get("formats", [])
    
    if not formats:
        requested = info.get("requested_formats", [])
        if requested:
            formats = requested
    
    logger.info(f"Processing {len(formats)} formats")
    
    # Try to get direct URL from info first
    if info.get('url') and 'm3u8' not in info.get('url', ''):
        result["type"] = "direct"
        result["url"] = info.get('url')
        result["format"] = info.get('ext', 'mp4')
        return result
    
    # Find progressive streams (video+audio combined)
    progressive_streams = []
    for f in formats:
        url = f.get("url")
        if not url:
            continue
            
        if "m3u8" in url:
            continue
            
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        
        if vcodec != "none" and acodec != "none":
            progressive_streams.append(f)
    
    if progressive_streams:
        # Sort by quality (height) and prefer mp4
        progressive_streams.sort(key=lambda x: (
            x.get("ext") == 'mp4',  # Prefer mp4
            x.get("height", 0) or 0  # Then by quality
        ), reverse=True)
        
        best = progressive_streams[0]
        
        result["type"] = "progressive"
        result["url"] = best.get("url")
        result["format"] = best.get("ext", "mp4")
        result["quality"] = best.get("height", "unknown")
        result["filesize"] = best.get("filesize", best.get("filesize_approx", 0))
        return result
    
    # Find separate video and audio streams
    video_streams = []
    audio_streams = []
    
    for f in formats:
        url = f.get("url")
        if not url:
            continue
            
        if "m3u8" in url:
            continue
            
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        
        if vcodec != "none" and (acodec == "none" or not acodec):
            video_streams.append(f)
        elif (acodec != "none" or acodec) and (vcodec == "none" or not vcodec):
            audio_streams.append(f)
    
    if video_streams and audio_streams:
        # Sort video by quality, audio by bitrate
        video_streams.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
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
        return result
    
    # Last resort: try any URL
    for f in formats:
        url = f.get("url")
        if url and "m3u8" not in url:
            result["type"] = "direct"
            result["url"] = url
            result["format"] = f.get("ext", "unknown")
            result["quality"] = f.get("height", f.get("quality", "unknown"))
            return result
    
    return None


@app.route("/debug/cookies", methods=["GET"])
def debug_cookies():
    """Debug endpoint to check cookies status"""
    cookies_valid = _has_valid_cookies_file()
    
    status = {
        "cookies_file": "cookies.txt",
        "file_exists": os.path.exists('cookies.txt'),
        "valid_format": cookies_valid,
    }
    
    if os.path.exists('cookies.txt'):
        status["file_size"] = os.path.getsize('cookies.txt')
        try:
            with open('cookies.txt', 'r') as f:
                lines = f.readlines()
                cookie_lines = [l for l in lines if l.strip() and not l.startswith('#')]
                status["total_lines"] = len(lines)
                status["valid_cookie_entries"] = len(cookie_lines)
                status["first_100_chars"] = lines[0][:100] if lines else ""
        except Exception as e:
            status["read_error"] = str(e)
    
    return jsonify(status)


@app.route("/debug/test", methods=["GET"])
def test_extraction():
    """Test endpoint for a specific video"""
    url = request.args.get("url", "https://youtu.be/qf8iHq3zhTU")
    
    results = []
    
    # Test with different clients
    for client in ['android', 'web', 'ios', 'tv']:
        try:
            ydl_opts = _build_ydl_opts(use_cookies=True, format_spec='best', client_type=client)
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                results.append({
                    "client": client,
                    "success": True,
                    "title": info.get("title"),
                    "format_count": len(info.get("formats", [])),
                })
        except Exception as e:
            results.append({
                "client": client,
                "success": False,
                "error": str(e)[:100]
            })
    
    return jsonify({
        "url": url,
        "video_id": extract_video_id(url),
        "test_results": results
    })


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint for Render"""
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
