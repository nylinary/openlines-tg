# Bitrix24 imbot proxy (MVP)

Minimal FastAPI service that:
- receives events from a Bitrix24 chat bot (registered in the Bitrix UI) at `/b24/imbot/events`
- logs incoming messages
- echoes message text back to the same dialog via `im.message.add`

Authentication uses **Bitrix24 OAuth local app** with tokens stored in Redis.

## Files

- `app/main.py` — FastAPI app, routes, startup/shutdown
- `app/config.py` — env config (Pydantic Settings)
- `app/bitrix.py` — Bitrix OAuth client + REST API caller
- `app/storage.py` — Redis dedupe + Bitrix token storage
- `app/schemas.py` — Pydantic payload schemas
- `app/logging.py` — JSON logging
- `docker-compose.yml`, `Dockerfile`, `.env.example`

## Setup

### 1. Bitrix24 local application

Create a **local application** in Bitrix24 Developer section:
- Set redirect URI to `https://YOUR_PUBLIC_URL/b24/oauth/callback`
- Note `client_id` and `client_secret`

### 2. Bitrix24 chat bot

Register a chat bot in the Bitrix24 UI (Developer → Chat Bots → Add).
- Set the bot's **Event Handler URL** to `https://YOUR_PUBLIC_URL/b24/imbot/events`
- Note the bot's **ID**, **CODE**, **CLIENT_ID**, and **NAME**

### 3. Environment

Copy `.env.example` → `.env` and fill in:
- `B24_DOMAIN`, `B24_CLIENT_ID`, `B24_CLIENT_SECRET`, `B24_REDIRECT_URI`
- `PUBLIC_DOMAIN`
- `B24_IMBOT_ID`, `B24_IMBOT_CODE`, `B24_IMBOT_NAME`, `B24_IMBOT_CLIENT_ID`

### 4. Run

```bash
docker compose up --build
```

App listens on `http://localhost:8000`.

### 5. OAuth install

Open `http://localhost:8000/b24/install` — it returns JSON with `auth_url`.
Open that URL in a browser and approve the app installation.
Bitrix will redirect to `/b24/oauth/callback` and tokens will be stored in Redis.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/b24/imbot/events` | Bitrix bot event handler (echo) |
| GET | `/b24/install` | Generate OAuth authorization URL |
| GET | `/b24/oauth/callback` | Handle OAuth redirect |

## Test curl

```bash
# Emulate a Bitrix bot event
curl -sS -X POST http://localhost:8000/b24/imbot/events \
  -H "Content-Type: application/json" \
  -d '{
    "data": {
      "PARAMS": {
        "DIALOG_ID": "123",
        "MESSAGE": "hello from curl"
      }
    }
  }'
```