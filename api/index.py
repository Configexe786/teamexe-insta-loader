"""
Instagram Media Downloader API (Optimized Clean Output)
-------------------------------------------------------
A serverless Vercel-compatible Python API that parses Instagram post/reel data 
and returns a clean, direct response containing only the top video and audio links.
Protected by a flat-file API key authentication system.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import re
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


def get_media_id(url: str) -> str | None:
    """Extracts the unique Instagram shortcode from reels, posts, or TV URLs."""
    # Regular expression pattern to match alphanumeric ID after /reel/, /p/, or /tv/
    match = re.search(r"/(?:reel|p|tv)/([A-Za-z0-9_-]+)", url)
    # Return the extracted ID string if a match is found, otherwise return None
    return match.group(1) if match else None


def fetch_payload(media_id: str) -> str:
    """Sends a POST request to Instagram's internal endpoint to fetch media data."""
    # Target internal Instagram route-definition endpoint URL
    url = "https://www.instagram.com/ajax/route-definition/"
    
    # Headers mimicking an official mobile Android client to prevent rate limits/blocks
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16; 2406ERN9CI) AppleWebKit/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-fb-lsd": "AdQ8qJYZezadBOYUt1mu-DRKm1I",
        "origin": "https://www.instagram.com",
        "referer": "https://www.instagram.com/",
    }
    
    # URL-encoded payload containing routing parameters and the targeted media shortcode
    data = urlencode({
        "route_url": f"/reel/{media_id}/?l=1",
        "__a": 1,
        "__comet_req": 117,
        "lsd": "AdQ8qJYZezadBOYUt1mu-DRKm1I",
    }).encode()
    
    # Create the HTTP request object with POST method and custom headers/data
    req = Request(url, data=data, headers=headers, method="POST")
    
    # Open the connection, execute the request, and decode response bytes into a string
    with urlopen(req) as resp:
        return resp.read().decode()


def parse_payload(data: str) -> dict:
    """Filters and structures raw chunks, returning only essential post info, top video, and audio."""
    # Instagram delivers data as chunked multi-part payloads separated by 'for (;;);'
    parts = data.split("for (;;);")
    
    # Initialize the clean base response dictionary structure
    out = {
        "post_info": {},
        "video": {},
        "audio": {},
        "thumbnails": [],
    }

    # Loop through each individual chunk part to locate media payload blocks
    for p in parts:
        try:
            # Attempt to convert each text chunk safely into a Python dictionary
            obj = json.loads(p.strip())
        except (json.JSONDecodeError, TypeError):
            # Skip chunks that aren't valid JSON syntax
            continue
            
        # Target specifically the chunk type labeled as 'preloader'
        if obj.get("__type") != "preloader":
            continue

        # Drill down deep nested JSON keys to extract public media metadata nodes
        m = (
            obj.get("result", {})
            .get("result", {})
            .get("data", {})
            .get("xig_polaris_media", {})
            .get("if_not_gated_logged_out", {})
        )

        # Extract primary post metrics (likes, dimensions, code, etc.)
        out["post_info"].update({
            k: m.get(k)
            for k in (
                "code",
                "pk",
                "original_width",
                "original_height",
                "like_count",
                "comment_count",
                "has_audio",
            )
        })
        # Extract post text description safely
        out["post_info"]["caption"] = (m.get("caption") or {}).get("text")
        
        # Extract creator profile details (username, internal user ID, profile picture)
        u = m.get("user", {})
        out["post_info"].update({
            "username": u.get("username"),
            "user_id": u.get("id"),
            "profile_pic_url": u.get("profile_pic_url"),
        })

        # Map available thumbnail variants for preview images
        out["thumbnails"] = [
            {"width": c.get("width"), "height": c.get("height"), "url": c.get("url")}
            for c in m.get("image_versions2", {}).get("candidates", [])
        ]
        
        # Map hashtags or topic category tags associated with the reel
        out["post_info"]["topics"] = [
            t.get("topic_name")
            for t in m.get("related_topic_pills", [])
            if t.get("topic_name")
        ]

        # Extract DASH streaming XML manifest string if available to parse precise quality streams
        manifest = m.get("video_dash_manifest")
        if not manifest:
            continue

        # Extract total video playback duration in seconds using regex matching
        if d := re.search(r'mediaPresentationDuration="PT([\d.]+)S"', manifest):
            out["post_info"]["duration"] = float(d.group(1))

        try:
            # Define XML namespace schema configuration for MPEG-DASH parsing
            ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
            
            video_candidates = []
            
            # Parse XML manifest elements to extract separate audio and high-res video streams
            for rep in ET.fromstring(manifest).findall(".//mpd:Representation", ns):
                dash_url = rep.findtext("mpd:BaseURL", namespaces=ns)
                mime_type = rep.get("mimeType") or ""
                
                # Check if the stream element contains audio information
                if "audio" in mime_type and not out["audio"]:
                    out["audio"] = {
                        "bandwidth_bps": int(rep.get("bandwidth") or 0) or None,
                        "codecs": rep.get("codecs"),
                        "sample_rate": rep.get("audioSamplingRate"),
                        "url": dash_url,
                    }
                # Check if the stream element contains high-resolution video formats
                elif dash_url and "video" in mime_type:
                    rep_height = int(rep.get("height") or 0)
                    video_candidates.append({
                        "quality": f"{rep_height}p" if rep_height else "HD",
                        "bandwidth_bps": int(rep.get("bandwidth") or 0) or 0,
                        "width": int(rep.get("width") or 0) or None,
                        "height": rep_height or None,
                        "mime_type": mime_type,
                        "codecs": rep.get("codecs"),
                        "url": dash_url,
                    })
            
            # Select only the highest quality video option available based on bandwidth/resolution
            if video_candidates:
                best_video = max(video_candidates, key=lambda x: x["bandwidth_bps"])
                out["video"] = best_video

        except ET.ParseError:
            # Fallback error mapping if XML parsing fails completely
            out["video"] = {"error": "Failed to parse XML manifest"}

    return out


def load_valid_keys() -> list[str]:
    """Reads the 'api_keys.txt' configuration file securely using absolute paths."""
    try:
        # Resolve the absolute path of the current directory and find root folder
        current_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(current_dir)
        keys_path = os.path.join(root_dir, "api_keys.txt")
        
        # Open file and parse each line into a clean list of allowed keys
        with open(keys_path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        # Return an empty list if configuration file is missing
        return []


class handler(BaseHTTPRequestHandler):
    """Manages incoming URL requests, checks authentication, and returns JSON."""

    def _send_json_response(self, status_code: int, payload: dict):
        """Helper method to construct HTTP headers and write JSON payloads back."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        """Handles incoming HTTP GET requests, extracts parameters, validates access, and outputs data."""
        # Parse the requested endpoint path and separate URL query variables
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)

        # Extract 'key' (API authentication) and 'url' (Instagram target link) parameters
        api_key = query_params.get("key", [None])[0]
        insta_url = query_params.get("url", [None])[0]

        # Step 1: Authenticate the provided API key against allowed keys list
        valid_keys = load_valid_keys()
        if not api_key or api_key not in valid_keys:
            self._send_json_response(401, {"error": "Unauthorized: Invalid or missing API key"})
            return

        # Step 2: Validate whether the target Instagram URL parameter was provided
        if not insta_url:
            self._send_json_response(400, {"error": "Bad Request: Missing 'url' parameter"})
            return

        # Step 3: Extract and validate media shortcode ID from the link
        media_id = get_media_id(insta_url)
        if not media_id:
            self._send_json_response(400, {"error": "Bad Request: Invalid Instagram URL format"})
            return

        # Step 4: Execute fetch and parse routines and send final clean JSON response to client
        try:
            raw_payload = fetch_payload(media_id)
            result = parse_payload(raw_payload)
            self._send_json_response(200, result)
        except Exception as exc:
            self._send_json_response(500, {"error": f"Internal Server Error: {str(exc)}"})
