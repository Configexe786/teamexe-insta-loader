"""
Instagram Media Downloader API (Optimized 1080p & Audio Fix)
------------------------------------------------------------
Serverless Vercel-compatible Python API that parses Instagram reels/posts,
extracts the highest available 1080p (or maximum) video stream, and maps 
the dedicated audio stream track.
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
    match = re.search(r"/(?:reel|p|tv)/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


def fetch_payload(media_id: str) -> str:
    """Sends a POST request to Instagram's internal endpoint to fetch media data."""
    url = "https://www.instagram.com/ajax/route-definition/"
    
    # Headers mimicking an official mobile Android client to prevent rate limits/blocks
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16; 2406ERN9CI) AppleWebKit/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-fb-lsd": "AdQ8qJYZezadBOYUt1mu-DRKm1I",
        "origin": "https://www.instagram.com",
        "referer": "https://www.instagram.com/",
    }
    
    data = urlencode({
        "route_url": f"/reel/{media_id}/?l=1",
        "__a": 1,
        "__comet_req": 117,
        "lsd": "AdQ8qJYZezadBOYUt1mu-DRKm1I",
    }).encode()
    
    req = Request(url, data=data, headers=headers, method="POST")
    with urlopen(req) as resp:
        return resp.read().decode()


def parse_payload(data: str) -> dict:
    """Filters chunks and extracts 1080p+ video and valid audio tracks from DASH XML."""
    parts = data.split("for (;;);")
    
    out = {
        "post_info": {},
        "video": {},
        "audio": {},
        "thumbnails": [],
    }

    for p in parts:
        try:
            obj = json.loads(p.strip())
        except (json.JSONDecodeError, TypeError):
            continue
            
        if obj.get("__type") != "preloader":
            continue

        m = (
            obj.get("result", {})
            .get("result", {})
            .get("data", {})
            .get("xig_polaris_media", {})
            .get("if_not_gated_logged_out", {})
        )

        # Extract primary post metrics
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
        out["post_info"]["caption"] = (m.get("caption") or {}).get("text")
        
        u = m.get("user", {})
        out["post_info"].update({
            "username": u.get("username"),
            "user_id": u.get("id"),
            "profile_pic_url": u.get("profile_pic_url"),
        })

        out["thumbnails"] = [
            {"width": c.get("width"), "height": c.get("height"), "url": c.get("url")}
            for c in m.get("image_versions2", {}).get("candidates", [])
        ]
        
        out["post_info"]["topics"] = [
            t.get("topic_name")
            for t in m.get("related_topic_pills", [])
            if t.get("topic_name")
        ]

        manifest = m.get("video_dash_manifest")
        if not manifest:
            continue

        if d := re.search(r'mediaPresentationDuration="PT([\d.]+)S"', manifest):
            out["post_info"]["duration"] = float(d.group(1))

        try:
            ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
            video_candidates = []
            audio_candidates = []
            
            for rep in ET.fromstring(manifest).findall(".//mpd:Representation", ns):
                dash_url = rep.findtext("mpd:BaseURL", namespaces=ns)
                mime_type = rep.get("mimeType") or ""
                
                if not dash_url:
                    continue

                # Handle audio components
                if "audio" in mime_type:
                    audio_candidates.append({
                        "bandwidth_bps": int(rep.get("bandwidth") or 0),
                        "codecs": rep.get("codecs"),
                        "sample_rate": rep.get("audioSamplingRate"),
                        "url": dash_url,
                    })
                # Handle video components (capturing 720p, 1080p, etc.)
                elif "video" in mime_type:
                    rep_height = int(rep.get("height") or 0)
                    rep_width = int(rep.get("width") or 0)
                    video_candidates.append({
                        "quality": f"{rep_height}p" if rep_height else "HD",
                        "bandwidth_bps": int(rep.get("bandwidth") or 0),
                        "width": rep_width or None,
                        "height": rep_height or None,
                        "mime_type": mime_type,
                        "codecs": rep.get("codecs"),
                        "url": dash_url,
                    })
            
            # Select the absolute highest resolution / bandwidth available (prioritizing 1080p+)
            if video_candidates:
                best_video = max(video_candidates, key=lambda x: (x["height"] or 0, x["bandwidth_bps"]))
                out["video"] = best_video

            # Select the highest quality audio track available
            if audio_candidates:
                best_audio = max(audio_candidates, key=lambda x: x["bandwidth_bps"])
                out["audio"] = best_audio

        except ET.ParseError:
            out["video"] = {"error": "Failed to parse XML manifest"}

    return out


def load_valid_keys() -> list[str]:
    """Reads the 'api_keys.txt' configuration file securely using absolute paths."""
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(current_dir)
        keys_path = os.path.join(root_dir, "api_keys.txt")
        
        with open(keys_path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


class handler(BaseHTTPRequestHandler):
    """Manages incoming URL requests, checks authentication, and returns JSON."""

    def _send_json_response(self, status_code: int, payload: dict):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)

        api_key = query_params.get("key", [None])[0]
        insta_url = query_params.get("url", [None])[0]

        valid_keys = load_valid_keys()
        if not api_key or api_key not in valid_keys:
            self._send_json_response(401, {"error": "Unauthorized: Invalid or missing API key"})
            return

        if not insta_url:
            self._send_json_response(400, {"error": "Bad Request: Missing 'url' parameter"})
            return

        media_id = get_media_id(insta_url)
        if not media_id:
            self._send_json_response(400, {"error": "Bad Request: Invalid Instagram URL format"})
            return

        try:
            raw_payload = fetch_payload(media_id)
            result = parse_payload(raw_payload)
            self._send_json_response(200, result)
        except Exception as exc:
            self._send_json_response(500, {"error": f"Internal Server Error: {str(exc)}"})
