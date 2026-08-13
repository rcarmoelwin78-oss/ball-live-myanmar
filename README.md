# Ball Live Myanmar

Telegram Web App for displaying approved live football events and playing provider-authorized HLS or YouTube streams.

## Local run

```powershell
python -m pip install -r requirements.txt
python server.py
```

Open `http://localhost:8080`.

## Add or change a match

Edit `matches.json`:

```json
{
  "id": 1,
  "authorized": true,
  "isLive": true,
  "league": "UEFA CHAMPIONS LEAGUE",
  "home": "Paris Saint-Germain",
  "away": "Aston Villa",
  "streamUrl": "https://provider.example/live.m3u8"
}
```

Use a stream URL supplied by your authorized provider. For YouTube, use `youtubeId` or `youtubeUrl` instead.

## Deploy

- Deploy `server.py` to Render, Railway, or a VPS. This is required for the subscription database and HLS proxy.
- `render.yaml` is included for Render. Add `TELEGRAM_BOT_TOKEN` and `ADMIN_API_TOKEN` as private environment variables.
- Keep the `members.db` database on a persistent disk (the supplied Render blueprint uses `/var/data`).
- Point the frontend API calls to the backend domain before production release.

## Monthly membership

The backend validates Telegram Web App `initData` on every protected API call. A member is active only until their stored `expires_at` timestamp. After reviewing a manual payment, activate 30 days with the admin endpoint:

```text
POST /api/admin/members/activate
Authorization: Bearer <ADMIN_API_TOKEN>
Content-Type: application/json

{"telegramUserId":"123456789","displayName":"User name","days":30}
```

Never expose `TELEGRAM_BOT_TOKEN` or `ADMIN_API_TOKEN` in HTML, GitHub, or a Telegram message.

Open `/admin` on the backend domain to use the manual member activation dashboard.
