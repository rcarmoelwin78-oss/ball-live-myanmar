"""Vercel serverless API for Ball Live Myanmar."""
import json
import os
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, request

ROOT = Path(__file__).resolve().parent.parent
MATCHES_FILE = ROOT / "matches.json"
USER_AGENT = "Mozilla/5.0 (compatible; BallLiveMyanmar/1.0)"

app = Flask(__name__)


def load_matches():
    with MATCHES_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def allowed_hosts():
    # The configured provider hostname works out of the box.  A deployment
    # can additionally supply ALLOWED_STREAM_HOSTS for its own provider hosts.
    configured = os.getenv("ALLOWED_STREAM_HOSTS", "")
    return {
        "popwellfox.s3.us-east-1.amazonaws.com",
        *{host.strip().lower() for host in configured.split(",") if host.strip()},
    }


def is_allowed(url):
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname and parsed.hostname.lower() in allowed_hosts()


def is_hls(url):
    return bool(url and ".m3u8" in url.lower())


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/matches")
def matches():
    return jsonify([match for match in load_matches() if match.get("authorized") and match.get("isLive")])


@app.get("/api/resolve/<int:match_id>")
def resolve(match_id):
    match = next((item for item in load_matches() if int(item.get("id", 0)) == match_id), None)
    if not match or not match.get("authorized"):
        return jsonify({"error": "Match not found"}), 404
    stream_url = match.get("streamUrl", "")
    if is_hls(stream_url):
        if not is_allowed(stream_url):
            return jsonify({"error": "Stream host is not allowed"}), 422
        return jsonify({"type": "hls", "streamUrl": f"/api/proxy?url={quote(stream_url, safe='')}"})
    if match.get("youtubeId") or match.get("youtubeUrl"):
        return jsonify({"type": "youtube", "youtube": match.get("youtubeId") or match.get("youtubeUrl")})
    return jsonify({"error": "No playable stream is configured"}), 422


@app.get("/api/proxy")
def proxy():
    target = request.args.get("url", "")
    if not is_allowed(target):
        return jsonify({"error": "Stream host is not allowed"}), 403
    try:
        upstream = requests.get(target, headers={"User-Agent": USER_AGENT}, timeout=20)
        upstream.raise_for_status()
    except requests.RequestException:
        return jsonify({"error": "Provider stream could not be reached securely"}), 502

    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    if is_hls(target) or "mpegurl" in content_type:
        base = target.rsplit("/", 1)[0] + "/"
        output = []
        for line in upstream.text.splitlines():
            item = line.strip()
            if not item or item.startswith("#"):
                output.append(line)
            else:
                absolute = urljoin(base, item)
                output.append(f"/api/proxy?url={quote(absolute, safe='')}" if is_allowed(absolute) else line)
        return Response("\n".join(output), mimetype="application/vnd.apple.mpegurl")
    return Response(upstream.content, mimetype=content_type)
