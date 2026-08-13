import json
import re
import time
import hmac
import hashlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, quote, urljoin, urlparse

import requests
import truststore
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS
from playwright.sync_api import sync_playwright

# Use certificates trusted by Windows as well as Python's standard trust chain.
# This keeps TLS verification enabled while supporting locally managed roots.
truststore.inject_into_ssl()

FIREBASE_URL = "https://bgm-live-db-default-rtdb.asia-southeast1.firebasedatabase.app/matches.json"
MATCHES_FILE = Path(__file__).with_name("matches.json")
PROVIDER_CONFIG_FILE = Path(__file__).with_name("provider_config.json")
MEMBERS_DB = Path(os.getenv("MEMBERS_DB", Path(os.getenv("DATA_DIR", Path(__file__).parent)) / "members.db"))
_provider_cache: list[dict] = []
_provider_cache_at = 0.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)


def database():
    connection = sqlite3.connect(MEMBERS_DB)
    connection.row_factory = sqlite3.Row
    connection.execute("""
        CREATE TABLE IF NOT EXISTS members (
            telegram_user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    return connection


def verify_telegram_user():
    """Validate Telegram Web App initData and return its user payload."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not init_data or not bot_token:
        return None
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(expected, received_hash):
        return None
    try:
        return json.loads(values["user"])
    except (KeyError, json.JSONDecodeError):
        return None


def current_member():
    """Return active Telegram member. Localhost is allowed for UI development."""
    if request.remote_addr in {"127.0.0.1", "::1"}:
        return {"id": "local-demo", "first_name": "Local Demo"}
    user = verify_telegram_user()
    if not user:
        return None
    with database() as connection:
        member = connection.execute(
            "SELECT expires_at FROM members WHERE telegram_user_id = ?", (str(user["id"]),)
        ).fetchone()
    if not member:
        return None
    expires_at = datetime.fromisoformat(member["expires_at"])
    return user if expires_at > datetime.now(UTC) else None


def member_required():
    member = current_member()
    if member:
        return member
    return jsonify({"error": "Subscription expired or inactive"}), 403


def site_base(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def allowed_stream_hosts() -> set[str]:
    """Read the provider-owned stream host allowlist from local configuration."""
    if not PROVIDER_CONFIG_FILE.exists():
        return set()
    with PROVIDER_CONFIG_FILE.open("r", encoding="utf-8") as file:
        config = json.load(file)
    return {host.lower() for host in config.get("allowedStreamHosts", [])}


def is_allowed_stream_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"https", "http"} and parsed.hostname and parsed.hostname.lower() in allowed_stream_hosts()


def is_m3u8(url: str) -> bool:
    return bool(url and ".m3u8" in url.lower())


def is_page_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return not is_m3u8(url) and any(
        host in lower
        for host in ("istreameast", "streamseast", "goal1.live", "streameast")
    )


def fetch_matches():
    """Load matches from the configured authorized provider or local catalog."""
    global _provider_cache, _provider_cache_at

    if PROVIDER_CONFIG_FILE.exists():
        with PROVIDER_CONFIG_FILE.open("r", encoding="utf-8") as file:
            provider = json.load(file)
        feed_url = provider.get("feedUrl", "").strip()
        refresh_seconds = max(30, int(provider.get("refreshSeconds", 60)))
        if feed_url:
            if _provider_cache and time.time() - _provider_cache_at < refresh_seconds:
                return _provider_cache
            headers = {"User-Agent": USER_AGENT}
            if provider.get("apiKey"):
                headers[provider.get("apiKeyHeader", "Authorization")] = provider["apiKey"]
            response = requests.get(feed_url, headers=headers, timeout=20)
            response.raise_for_status()
            payload = response.json()
            entries = payload.get("matches", payload) if isinstance(payload, dict) else payload
            if not isinstance(entries, list):
                raise ValueError("Provider feed must be a JSON list or contain a 'matches' list")
            _provider_cache = [
                {**match, "authorized": True}
                for match in entries if isinstance(match, dict)
            ]
            _provider_cache_at = time.time()
            return _provider_cache

    if MATCHES_FILE.exists():
        with MATCHES_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return [m for m in (data or []) if m]

    response = requests.get(FIREBASE_URL, timeout=20)
    response.raise_for_status()
    data = response.json()
    return [m for m in (data or []) if m]


def extract_m3u8(page_url: str) -> str | None:
    found_streams: list[str] = []

    def capture(request):
        url = request.url
        if ".m3u8" in url and url not in found_streams:
            found_streams.append(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.on("request", capture)

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

            for selector in (
                "button:has-text('Stream')",
                "button:has-text('Server')",
                "a:has-text('Stream')",
                "a:has-text('Server')",
                "a:has-text('HD')",
                "[class*='stream']",
                "[class*='server']",
            ):
                try:
                    page.locator(selector).first.click(timeout=1500)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

            for frame in page.frames:
                try:
                    frame.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            if found_streams:
                return found_streams[0]

            html = page.content()
            match = re.search(r'https?://[^\s"\']+\.m3u8[^\s"\']*', html)
            if match:
                return match.group(0)

            for iframe in page.query_selector_all("iframe[src]"):
                src = iframe.get_attribute("src")
                if not src:
                    continue
                iframe_url = urljoin(page_url, src)
                try:
                    page.goto(iframe_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(4000)
                    if found_streams:
                        return found_streams[0]
                except Exception:
                    continue
        finally:
            page.remove_listener("request", capture)
            browser.close()

    return None


def proxy_request(target_url: str, referer: str | None = None) -> Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    if referer:
        headers["Referer"] = referer

    upstream = requests.get(target_url, headers=headers, timeout=30, stream=True)
    content_type = upstream.headers.get("Content-Type", "application/octet-stream")

    if "mpegurl" in content_type or target_url.endswith(".m3u8") or ".m3u8" in target_url:
        text = upstream.text
        base = target_url.rsplit("/", 1)[0] + "/"
        rewritten = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                rewritten.append(line)
                continue
            if stripped.startswith("#"):
                # HLS directives such as EXT-X-KEY can contain URI="...".
                # Route those resources through the same allowlisted proxy too.
                def rewrite_uri(match):
                    absolute = urljoin(base, match.group(1))
                    if not is_allowed_stream_url(absolute):
                        return match.group(0)
                    return f'URI="/proxy?url={quote(absolute, safe="")}"'

                rewritten.append(re.sub(r'URI="([^"]+)"', rewrite_uri, line))
                continue
            absolute = urljoin(base, stripped)
            if is_allowed_stream_url(absolute):
                rewritten.append(f"/proxy?url={quote(absolute, safe='')}")
            else:
                rewritten.append(line)

        body = "\n".join(rewritten)
        return Response(body, status=upstream.status_code, mimetype="application/vnd.apple.mpegurl")

    return Response(
        upstream.content,
        status=upstream.status_code,
        mimetype=content_type,
    )


@app.get("/")
def home():
    return send_from_directory(".", "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/session")
def session():
    """Return the current Telegram member's subscription state."""
    user = verify_telegram_user()
    if not user:
        return jsonify({"active": False, "error": "Telegram verification failed"}), 401
    with database() as connection:
        member = connection.execute(
            "SELECT expires_at FROM members WHERE telegram_user_id = ?", (str(user["id"]),)
        ).fetchone()
    if not member:
        return jsonify({"active": False, "error": "No active subscription"}), 403
    expires_at = datetime.fromisoformat(member["expires_at"])
    return jsonify({"active": expires_at > datetime.now(UTC), "expiresAt": expires_at.isoformat()})


@app.post("/api/admin/members/activate")
def activate_member():
    """Manual-payment admin action: activate a Telegram user for a number of days."""
    if not os.getenv("ADMIN_API_TOKEN") or request.headers.get("Authorization") != f"Bearer {os.getenv('ADMIN_API_TOKEN')}":
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    user_id = str(body.get("telegramUserId", "")).strip()
    days = int(body.get("days", 30))
    if not user_id or days < 1 or days > 366:
        return jsonify({"error": "telegramUserId and valid days are required"}), 400
    now = datetime.now(UTC)
    with database() as connection:
        existing = connection.execute(
            "SELECT expires_at FROM members WHERE telegram_user_id = ?", (user_id,)
        ).fetchone()
        base = now
        if existing:
            existing_expiry = datetime.fromisoformat(existing["expires_at"])
            if existing_expiry > now:
                base = existing_expiry
        expiry = base + timedelta(days=days)
        connection.execute(
            "INSERT INTO members (telegram_user_id, display_name, expires_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET display_name=excluded.display_name, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (user_id, str(body.get("displayName", "")), expiry.isoformat(), now.isoformat()),
        )
    return jsonify({"telegramUserId": user_id, "expiresAt": expiry.isoformat()})


@app.get("/api/matches")
def matches():
    """Only publish events explicitly marked as authorized in the catalog."""
    access = member_required()
    if not isinstance(access, dict):
        return access
    try:
        published = [
            match for match in fetch_matches()
            if match.get("authorized") is True and match.get("isLive", True)
        ]
        return jsonify(published)
    except requests.RequestException:
        return jsonify({"error": "Match list is temporarily unavailable"}), 503


@app.get("/api/resolve/<int:match_id>")
def resolve_match(match_id: int):
    access = member_required()
    if not isinstance(access, dict):
        return access
    matches = fetch_matches()
    match = next((m for m in matches if int(m.get("id", 0)) == match_id), None)
    if not match or match.get("authorized") is not True:
        return jsonify({"error": "Match not found"}), 404

    stream_url = match.get("streamUrl") or match.get("embedUrl")

    if is_m3u8(stream_url):
        if not is_allowed_stream_url(stream_url):
            return jsonify({"error": "Stream host is not in the provider allowlist"}), 422
        proxied = f"/proxy?url={quote(stream_url, safe='')}"
        return jsonify({"streamUrl": proxied, "type": "hls"})
    if match.get("youtubeId") or match.get("youtubeUrl"):
        return jsonify({"type": "youtube", "youtube": match.get("youtubeId") or match.get("youtubeUrl")})

    return jsonify({"error": "No authorized playable stream is available"}), 422


@app.get("/proxy")
def proxy():
    access = member_required()
    if not isinstance(access, dict):
        return access
    target_url = request.args.get("url")
    referer = request.args.get("referer")
    if not target_url:
        return jsonify({"error": "Missing url"}), 400
    if not is_allowed_stream_url(target_url):
        return jsonify({"error": "Stream host is not allowed"}), 403
    try:
        return proxy_request(target_url, referer)
    except requests.RequestException:
        return jsonify({"error": "Provider stream could not be reached securely"}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
