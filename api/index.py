"""
Instagram Media Downloader API
-----------------------------
A serverless Vercel-compatible Python API that parses Instagram post/reel data 
and returns structured JSON info including video links, DASH qualities, and audio specs.
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
    """Extracts the unique Instagram media shortcode (ID) from reels, posts, or TV URLs."""
    # Regular expression pattern to capture the alphanumeric media shortcode after /reel/, /p/, or /tv/
    match = re.search(r"/(?:reel|p|tv)/([A-Za-z0-9_-]+)", url)
    # Return the matched group 1 (the shortcode) if a match is found, otherwise return None
    return match.group(1) if match else None


def fetch_payload(media_id: str) -> str:
    """Sends a POST request to Instagram's internal route definition endpoint to fetch media metadata."""
    # Instagram internal ajax route definition endpoint used to fetch preloader data chunks
    url = "https://www.instagram.com/ajax/route-definition/"
    
    # Custom headers imitating a mobile Android app browser to prevent immediate blocking/rate-limiting
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16; 2406ERN9CI) AppleWebKit/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-fb-lsd": "AdQ8qJYZezadBOYUt1mu-DRKm1I",
        "origin": "https://www.instagram.com",
        "referer": "https://www.instagram.com/",
    }
    
    # Form data payload required by Instagram's internal routing system for the specific media ID
    data = urlencode({
        "route_url": f"/reel/{media_id}/?l=1",
        "__a": 1,
        "__comet_req": 117,
        "lsd": "AdQ8qJYZezadBOYUt1mu-DRKm1I",
    }).encode()
    
    # Construct the HTTP POST request object with target URL, payload bytes, headers, and method
    req = Request(url, data=data, headers=headers, method="POST")
    
    # Open the HTTP connection and read/decode the response payload text string
    with urlopen(req) as resp:
        return resp.read().decode()


def parse_payload(data: str) -> dict:
    """Parses Instagram's multi-part streaming JSON payload and extracts media details."""
    # Instagram returns chunked javascript/JSON data separated by the multi-part delimiter string
    parts = data.split("for (;;);")
    
    # Initialize the structured dictionary template for final output data
    out = {
        "post_info": {},
        "video_links": [],
        "dash_qualities": [],
        "thumbnails": [],
        "audio_info": {},
    }

    # Iterate through each split part to locate the chunk containing the preloader data object
    for p in parts:
        try:
            # Safely parse text chunk into a Python dictionary object
            obj = json.loads(p.strip())
        except (json.JSONDecodeError, TypeError):
            # Skip chunks that aren't valid JSON syntax
            continue
            
        # Filter chunks to target only the 'preloader' type payload containing media stats
        if obj.get("__type") != "preloader":
            continue

        # Navigate deeply nested dictionary keys to locate public media metadata nodes safely
        m = (
            obj.get("result", {})
            .get("result", {})
            .get("data", {})
            .get("xig_polaris_media", {})
            .get("if_not_gated_logged_out", {})
        )

        # Extract basic general post metrics and information via dictionary comprehension
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
        # Extract post text caption safely with fallback to empty dict
        out["post_info"]["caption"] = (m.get("caption") or {}).get("text")
        
        # Extract uploader user profile details (username, internal user ID, avatar url)
        u = m.get("user", {})
        out["post_info"].update({
            "username": u.get("username"),
            "user_id": u.get("id"),
            "profile_pic_url": u.get("profile_pic_url"),
        })
        
        # Map available progressive direct download video links
        out["video_links"] = [
            {"type": v.get("type"), "url": v.get("url")}
            for v in m.get("video_versions", [])
        ]
        
        # Map available image thumbnail resolution variants
        out["thumbnails"] = [
            {"width": c.get("width"), "height": c.get("height"), "url": c.get("url")}
            for c in m.get("image_versions2", {}).get("candidates", [])
        ]
        
        # Map related content topics/pills associated with the post
        out["post_info"]["topics"] = [
            t.get("topic_name")
            for t in m.get("related_topic_pills", [])
            if t.get("topic_name")
        ]

        # Extract DASH manifest string used for adaptive video streaming qualities
        manifest = m.get("video_dash_manifest")
        if not manifest:
            continue

        # Use regex to find total video duration time in seconds from manifest attributes
        if d := re.search(r'mediaPresentationDuration="PT([\d.]+)S"', manifest):
            out["post_info"]["duration"] = float(d.group(1))

        try:
            # XML Namespace dictionary required to parse MPEG-DASH XML manifests accurately
            ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
            
            # Parse XML manifest string and find all media stream representation nodes
            for rep in ET.fromstring(manifest).findall(".//mpd:Representation", ns):
                dash_url = rep.findtext("mpd:BaseURL", namespaces=ns)
                mime_type = rep.get("mimeType") or ""
                
                # Filter representation nodes belonging to audio data streams
                if "audio" in mime_type:
                    out["audio_info"] = {
                        "bandwidth_bps": int(rep.get("bandwidth") or 0) or None,
                        "codecs": rep.get("codecs"),
                        "sample_rate": rep.get("audioSamplingRate"),
                        "url": dash_url,
                    }
                # Filter representation nodes belonging to high-resolution video data streams
                elif dash_url and "video" in mime_type:
                    out["dash_qualities"].append({
                        "bandwidth_bps": int(rep.get("bandwidth") or 0) or None,
                        "width": int(rep.get("width") or 0) or None,
                        "height": int(rep.get("height") or 0) or None,
                        "mime_type": mime_type,
                        "codecs": rep.get("codecs"),
                        "quality_label": rep.get("FBQualityLabel"),
                        "url": dash_url,
                    })
        except ET.ParseError:
            # Fallback error mapping if XML data structure formatting fails
            out["dash_qualities"] = [{"error": "Failed to parse XML manifest"}]

    return out


def load_valid_keys() -> list[str]:
    """Safely loads allowed API keys from the 'api_keys.txt' flat file using absolute paths."""
    try:
        # Determine the absolute system directory path where this current Python script file resides
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # Navigate one directory level up to locate the root repository directory
        root_dir = os.path.dirname(current_dir)
        # Join root path with the target configuration text filename
        keys_path = os.path.join(root_dir, "api_keys.txt")
        
        # Open and read file line-by-line, stripping whitespace and filtering out empty lines
        with open(keys_path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        # Return an empty list gracefully if the key file is missing on the server
        return []


class handler(BaseHTTPRequestHandler):
    """Vercel Serverless HTTP Request Handler class for processing inbound API query endpoints."""

    def _send_json_response(self, status_code: int, payload: dict):
        """Helper method to construct standard HTTP headers and write serialized JSON responses back."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        """Handles incoming HTTP GET requests, parses parameters, checks security keys, and returns data."""
        # Parse full URL string into URL-component pieces
        parsed_path = urlparse(self.path)
        # Extract dictionary mapping of URL query string parameters
        query_params = parse_qs(parsed_path.query)

        # Extract target parameters ('key' for authentication, 'url' for target content link)
        api_key = query_params.get("key", [None])[0]
        insta_url = query_params.get("url", [None])[0]

        # Step 1: Validate API Key authentication against loaded keys list
        valid_keys = load_valid_keys()
        if not api_key or api_key not in valid_keys:
            # Return HTTP 401 Unauthorized status if key verification fails
            self._send_json_response(401, {"error": "Unauthorized: Invalid or missing API key"})
            return

        # Step 2: Validate the presence of the Instagram target URL query parameter
        if not insta_url:
            # Return HTTP 400 Bad Request status if query variable is absent
            self._send_json_response(400, {"error": "Bad Request: Missing 'url' parameter"})
            return

        # Step 3: Extract and validate the core media shortcode ID from the link string
        media_id = get_media_id(insta_url)
        if not media_id:
            # Return HTTP 400 Bad Request status if shortcode pattern cannot be parsed
            self._send_json_response(400, {"error": "Bad Request: Invalid Instagram URL format"})
            return

        # Step 4: Execute fetch and parse workflow to retrieve target payload data
        try:
            raw_payload = fetch_payload(media_id)
            result = parse_payload(raw_payload)
            # Return HTTP 200 OK status along with structured JSON output data
            self._send_json_response(200, result)
        except Exception as exc:
            # Return HTTP 500 status block if network failures or parser exceptions occur unexpectedly
            self._send_json_response(500, {"error": f"Internal Server Error: {str(exc)}"})
