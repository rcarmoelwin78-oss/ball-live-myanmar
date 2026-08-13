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
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
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
    connection.execute("""
        CREATE TABLE IF NOT EXISTS payment_requests (
            telegram_user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS customer_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id TEXT NOT NULL,
            message_type TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            file_id TEXT NOT NULL DEFAULT '',
            received_at TEXT NOT NULL
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
    # Only permit unauthenticated access when explicitly enabled for a local
    # developer machine. Render/Vercel may use 127.0.0.1 internally, so the
    # host check is required in addition to the remote address.
    local_hosts = {"127.0.0.1:8080", "localhost:8080", "[::1]:8080"}
    if (
        os.getenv("ALLOW_LOCAL_DEMO") == "true"
        and request.remote_addr in {"127.0.0.1", "::1"}
        and request.host in local_hosts
    ):
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


def admin_authorized():
    token = os.getenv("ADMIN_API_TOKEN", "")
    return bool(token) and hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}")


def telegram_api(method: str, payload: dict):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Telegram bot is not configured")
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


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


@app.get("/admin")
def admin_dashboard():
    return send_from_directory(".", "admin.html")


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
    if not admin_authorized():
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
    web_app_url = os.getenv("WEB_APP_URL", "").strip()
    delivery = "activated"
    if web_app_url:
        try:
            telegram_api("sendMessage", {
                "chat_id": user_id,
                "text": (
                    "✅ Live Pass ကိုဖွင့်ပေးပြီးပါပြီ!\n\n"
                    f"သက်တမ်းကုန်ချိန်: {expiry.astimezone().strftime('%Y-%m-%d %H:%M')}\n\n"
                    "အောက်က 🔴 Watch Live ကိုနှိပ်ပြီး ပွဲတွေကြည့်နိုင်ပါပြီ။"
                ),
                "reply_markup": {
                    "inline_keyboard": [[{
                        "text": "🔴 Watch Live",
                        "web_app": {"url": web_app_url},
                    }]],
                },
            })
            delivery = "watch link sent"
        except (RuntimeError, requests.RequestException):
            delivery = "activated, but watch link could not be sent"
    return jsonify({"telegramUserId": user_id, "expiresAt": expiry.isoformat(), "delivery": delivery})


@app.post("/api/admin/telegram/webhook")
def configure_telegram_webhook():
    """Point the bot at this service without exposing the BotFather token."""
    if not admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"error": "Set TELEGRAM_WEBHOOK_SECRET in Render first"}), 422
    try:
        telegram_api("setWebhook", {
            "url": request.url_root.rstrip("/") + "/api/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["message"],
        })
    except (RuntimeError, requests.RequestException):
        return jsonify({"error": "Could not connect the Telegram bot"}), 502
    return jsonify({"ok": True})


@app.get("/api/admin/payment-requests")
def payment_requests():
    """List users who have messaged the bot for simple manual activation."""
    if not admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    with database() as connection:
        rows = connection.execute("""
            SELECT p.telegram_user_id, p.display_name, p.received_at,
                   m.message_type, m.body AS latest_message, m.file_id
            FROM payment_requests p
            LEFT JOIN customer_messages m ON m.id = (
                SELECT id FROM customer_messages
                WHERE telegram_user_id = p.telegram_user_id
                ORDER BY id DESC LIMIT 1
            )
            ORDER BY p.received_at DESC LIMIT 100
        """).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/admin/payment-requests/<telegram_user_id>/photo")
def payment_photo(telegram_user_id: str):
    """Return a Telegram-uploaded payment image to the authenticated admin only."""
    if not admin_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    file_id = request.args.get("fileId", "").strip()
    if not file_id:
        return jsonify({"error": "Screenshot not found"}), 404
    try:
        metadata = telegram_api("getFile", {"file_id": file_id})
        file_path = metadata.get("result", {}).get("file_path", "")
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not file_path or not token:
            raise RuntimeError("Screenshot unavailable")
        image = requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=20)
        image.raise_for_status()
        return Response(image.content, content_type=image.headers.get("Content-Type", "image/jpeg"))
    except (requests.RequestException, RuntimeError):
        return jsonify({"error": "Could not load screenshot"}), 502


@app.post("/api/telegram/webhook")
def telegram_webhook():
    """Show a short, button-led payment flow and record interested users."""
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not secret or not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret):
        return jsonify({"error": "Unauthorized"}), 401
    update = request.get_json(silent=True) or {}
    callback = update.get("callback_query") or {}
    message = update.get("message") or callback.get("message") or {}
    sender = callback.get("from") or message.get("from") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip().lower()
    if sender.get("id") and chat.get("id"):
        name = " ".join(part for part in [sender.get("first_name", ""), sender.get("last_name", "")] if part).strip()
        with database() as connection:
            connection.execute(
                "INSERT INTO payment_requests (telegram_user_id, display_name, received_at) VALUES (?, ?, ?) "
                "ON CONFLICT(telegram_user_id) DO UPDATE SET display_name=excluded.display_name, received_at=excluded.received_at",
                (str(sender["id"]), name, datetime.now(UTC).isoformat()),
            )
            if message.get("photo"):
                file_id = message["photo"][-1].get("file_id", "")
                message_type, body = "photo", message.get("caption", "")
            elif message.get("document"):
                file_id = message["document"].get("file_id", "")
                message_type, body = "document", message.get("caption", "")
            else:
                file_id = ""
                message_type, body = "text", message.get("text", "")
            if not callback and (body or file_id):
                connection.execute(
                    "INSERT INTO customer_messages (telegram_user_id, message_type, body, file_id, received_at) VALUES (?, ?, ?, ?, ?)",
                    (str(sender["id"]), message_type, body, file_id, datetime.now(UTC).isoformat()),
                )
    if not sender.get("id") or not chat.get("id"):
        return jsonify({"ok": True})

    channel_url = os.getenv("CHANNEL_URL", "").strip()
    web_app_url = os.getenv("WEB_APP_URL", "").strip()
    kpay_number = os.getenv("KPAY_NUMBER", "").strip()
    kpay_account = os.getenv("KPAY_ACCOUNT_NAME", "").strip()
    keyboard = [[{"text": "🔴 Live ဝယ်မည်", "callback_data": "buy_live"}]]
    if web_app_url.startswith("https://"):
        keyboard[0].append({"text": "📺 Live ကြည့်မည်", "web_app": {"url": web_app_url}})
    if channel_url.startswith("https://t.me/"):
        keyboard.append([{"text": "📢 Channel ဝင်မည်", "url": channel_url}])

    if callback.get("id"):
        telegram_api("answerCallbackQuery", {"callback_query_id": callback["id"]})
    if callback.get("data") == "buy_live" or text in {"live ဝယ်မည်", "ဝယ်မည်", "buy"}:
        payment_text = "\n".join(part for part in [
            "💳 တစ်လ Live Pass — 5,000 Ks (30 days)",
            f"KPay: {kpay_number}" if kpay_number else "KPay နံပါတ်ကို admin ထံမေးပါ။",
            f"အမည်: {kpay_account}" if kpay_account else "",
            "",
            "1. အထက်က KPay သို့ငွေလွှဲပါ",
            "2. Payment screenshot ကို ဒီ bot ထဲသို့ပို့ပါ",
            "3. Admin အတည်ပြုပြီးလျှင် Watch Live ခလုတ်ကို bot က ပြန်ပို့ပေးပါမယ်။",
        ]).strip()
        telegram_api("sendMessage", {
            "chat_id": chat["id"],
            "text": payment_text,
            "reply_markup": {"inline_keyboard": keyboard},
        })
    elif text in {"/start", "/menu", "menu", "မင်္ဂလာပါ"}:
        telegram_api("sendMessage", {
            "chat_id": chat["id"],
            "text": "⚽ Ball General Live Myanmar မှ ကြိုဆိုပါတယ်။\n\nလိုချင်တာကို အောက်ကခလုတ်တစ်ခုနှိပ်ပါ။",
            "reply_markup": {"inline_keyboard": keyboard},
        })
    elif message.get("photo") or message.get("document"):
        telegram_api("sendMessage", {
            "chat_id": chat["id"],
            "text": "✅ Payment screenshot ကိုလက်ခံပြီးပါပြီ။ Admin စစ်ဆေးအတည်ပြုပြီးလျှင် 🔴 Watch Live ခလုတ်ကို ဒီ bot မှာပြန်ပို့ပေးပါမယ်။",
            "reply_markup": {"inline_keyboard": keyboard},
        })
    elif text:
        telegram_api("sendMessage", {
            "chat_id": chat["id"],
            "text": "Live Pass ဝယ်လိုပါက 🔴 Live ဝယ်မည် ကိုနှိပ်ပါ။ ကြည့်လိုပါက 📺 Live ကြည့်မည် ကိုနှိပ်ပါ။",
            "reply_markup": {"inline_keyboard": keyboard},
        })
    return jsonify({"ok": True})


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
