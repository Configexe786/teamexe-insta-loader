from http.server import BaseHTTPRequestHandler
import json
import re
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


def get_media_id(url):
  match = re.search(r"/(?:reel|p|tv)/([A-Za-z0-9_-]+)", url)
  return match.group(1) if match else None


def fetch_payload(media_id):
  url = "https://www.instagram.com/ajax/route-definition/"
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Linux; Android 16; 2406ERN9CI) AppleWebKit/537.36"
      ),
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


def parse_payload(data):
  parts = data.split("for (;;);")
  out = {
      "post_info": {},
      "video_links": [],
      "dash_qualities": [],
      "thumbnails": [],
      "audio_info": {},
  }

  for p in parts:
    try:
      obj = json.loads(p.strip())
    except:
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
    out["video_links"] = [
        {"type": v.get("type"), "url": v.get("url")}
        for v in m.get("video_versions", [])
    ]
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
      for rep in ET.fromstring(manifest).findall(".//mpd:Representation", ns):
        url = rep.findtext("mpd:BaseURL", namespaces=ns)
        if "audio" in (rep.get("mimeType") or ""):
          out["audio_info"] = {
              "bandwidth_bps": int(rep.get("bandwidth") or 0) or None,
              "codecs": rep.get("codecs"),
              "sample_rate": rep.get("audioSamplingRate"),
              "url": url,
          }
        elif url and "video" in (rep.get("mimeType") or ""):
          out["dash_qualities"].append({
              "bandwidth_bps": int(rep.get("bandwidth") or 0) or None,
              "width": int(rep.get("width") or 0) or None,
              "height": int(rep.get("height") or 0) or None,
              "mime_type": rep.get("mimeType"),
              "codecs": rep.get("codecs"),
              "quality_label": rep.get("FBQualityLabel"),
              "url": url,
          })
    except ET.ParseError:
      out["dash_qualities"] = [{"error": "Failed to parse manifest"}]

  return out


def load_valid_keys():
  try:
    with open("api_keys.txt", "r") as f:
      return [line.strip() for line in f if line.strip()]
  except FileNotFoundError:
    return []


class handler(BaseHTTPRequestHandler):

  def do_GET(self):
    parsed_path = urlparse(self.path)
    query_params = parse_qs(parsed_path.query)

    # Extract API Key and URL parameter
    api_key = query_params.get("key", [None])[0]
    insta_url = query_params.get("url", [None])[0]

    # Validate API Key
    valid_keys = load_valid_keys()
    if not api_key or api_key not in valid_keys:
      self.send_response(401)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(
          json.dumps({"error": "Unauthorized: Invalid or missing API key"}).encode(
              "utf-8"
          )
      )
      return

    # Validate Instagram URL
    if not insta_url:
      self.send_response(400)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(
          json.dumps({"error": "Bad Request: Missing 'url' parameter"}).encode(
              "utf-8"
          )
      )
      return

    media_id = get_media_id(insta_url)
    if not media_id:
      self.send_response(400)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(
          json.dumps({"error": "Bad Request: Invalid Instagram URL"}).encode(
              "utf-8"
          )
      )
      return

    try:
      raw = fetch_payload(media_id)
      result = parse_payload(raw)
      self.send_response(200)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(
          json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8")
      )
    except Exception as e:
      self.send_response(500)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(
          json.dumps({"error": str(e)}).encode("utf-8")
      )
