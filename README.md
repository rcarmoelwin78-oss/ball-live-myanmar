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

- Deploy the frontend and serverless API to Vercel from this GitHub repository.
- In Vercel Project Settings → Environment Variables, set `ALLOWED_STREAM_HOSTS` to the comma-separated provider stream hostnames.
- Redeploy after changing environment variables.

For high-traffic HLS proxying, host `server.py` on a persistent backend such as Render, Railway, or a VPS and keep Vercel for the frontend.
