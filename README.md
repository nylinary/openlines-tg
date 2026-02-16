# Telegram ↔ Bitrix24 OpenLines proxy (MVP)

Minimal FastAPI proxy that:
- receives Telegram updates (polling via `getUpdates` by default)
- sends messages to Bitrix24 OpenLines via `imconnector.send.messages`
- receives Bitrix connector callbacks on `/b24/handler`
- sends outgoing messages back to Telegram with best-effort Bitrix delivery/reading statuses

Important: `imconnector.*` requires **Bitrix24 application (OAuth)**. Incoming user webhooks (`.../rest/<user>/<token>/`) are not accepted for these methods.

## Files

- `app/main.py` — FastAPI app, routes, startup/shutdown
- `app/config.py` — env config (Pydantic Settings)
- `app/bitrix.py` — Bitrix OAuth client + register/activate/send/status
- `app/telegram.py` — Telegram client
- `app/storage.py` — Redis dedupe + mapping + Bitrix token storage
- `app/schemas.py` — Pydantic payload subsets
- `app/logging.py` — JSON logging
- `docker-compose.yml`, `Dockerfile`, `.env.example`

## Run (docker compose)

1) Create `.env` from example:

- copy `.env.example` → `.env`
- fill: `B24_DOMAIN`, `B24_CLIENT_ID`, `B24_CLIENT_SECRET`, `B24_REDIRECT_URI`, `B24_LINE_ID`, `B24_HANDLER_SECRET`, `TG_BOT_TOKEN`

2) Start:

```bash
docker compose up --build
```

App listens on `http://localhost:8000`.

## Bitrix OAuth install (local app)

1) In Bitrix24 create a **local application** and set its redirect URI to:

`B24_REDIRECT_URI=https://YOUR_PUBLIC_URL/b24/oauth/callback`

2) Open in browser:

- `http://localhost:8000/b24/install`

It returns JSON with `auth_url`. Open that `auth_url` in browser, approve installation.

3) After redirect, Bitrix will call `/b24/oauth/callback?code=...&state=...` and the app will store tokens in Redis and attempt `imconnector.register/activate`.

## Telegram

By default the app uses polling (`TG_USE_POLLING=1`). No Telegram webhook setup is required.

## Bitrix handler

Configure Bitrix connector callback URL to your public URL:

`https://YOUR_PUBLIC_URL/b24/handler?secret=...`

where `secret` equals `B24_HANDLER_SECRET`.

## Test curls

### 1) Emulate Telegram update (webhook endpoint still exists)

If you keep `/tg/webhook` enabled, it will validate secret only when `TG_WEBHOOK_SECRET` is set.

```bash
curl -sS -X POST http://localhost:8000/tg/webhook \
  -H "Content-Type: application/json" \
  -H "X-Telegram-Bot-Api-Secret-Token: ${TG_WEBHOOK_SECRET}" \
  -d '{
    "update_id": 1000001,
    "message": {
      "message_id": 10,
      "from": {"id": 123456, "first_name": "Test", "last_name": "User"},
      "chat": {"id": 123456, "type": "private"},
      "text": "hello from curl"
    }
  }'
```

### 2) Emulate Bitrix outgoing event

```bash
curl -sS -X POST "http://localhost:8000/b24/handler?secret=${B24_HANDLER_SECRET}" \
  -H "Content-Type: application/json" \
  -d '{
    "event": "ONIMCONNECTORMESSAGEADD",
    "data": {
      "chat": {"id": "tg:123456"},
      "message": {"id": "b24:1", "text": "reply from Bitrix (emulated)"}
    }
  }'
```

## Notes

- Idempotency:
  - Telegram updates deduped by `update_id` in Redis.
  - Bitrix handler deduped by `external_chat_id + message_id` (fallback: hash(text)).
- If `imconnector.send.messages` fails due to portal-specific payload requirements, the app logs the request payload and error.