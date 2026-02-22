# Bitrix24 imbot proxy + AI chatbot (myryba.ru)

FastAPI service that powers a Bitrix24 chat bot for the myryba.ru seafood store:

- **Receives events** from a Bitrix24 chat bot at `/b24/imbot/events`
- **AI-powered responses** via OpenAI — product search, FAQ, recommendations
- **Product catalog** scraped from myryba.ru (Tilda Store API) with background refresh
- **Operator transfer** — keyword "оператор" hands off to a live agent
- **Graceful degradation** — works as echo-bot if OpenAI key is not configured

## Architecture

```
Bitrix24 Chat ──► /b24/imbot/events ──► AI Chat Handler ──► OpenAI
                                              │
                                        Product Catalog ◄── PostgreSQL
                                         (scraped from       (source of truth)
                                          myryba.ru)
                                              │
                                            Redis
                                         (cache, chat history,
                                          OAuth tokens)
```

## Files

| File | Description |
|------|-------------|
| `app/main.py` | FastAPI app, routes, startup, background scraper loop |
| `app/config.py` | Env config (Pydantic Settings) |
| `app/bitrix.py` | Bitrix OAuth client + webhook REST caller |
| `app/scraper.py` | myryba.ru product scraper (Tilda Store API) |
| `app/database.py` | Async SQLAlchemy PostgreSQL layer |
| `app/models.py` | SQLAlchemy ORM models (Product, ScrapeMeta, ChatMessage) |
| `app/ai_chat.py` | AI chat handler — intent detection, search, GPT orchestration |
| `app/storage.py` | Redis: dedupe, OAuth tokens, conversation history (PG fallback) |
| `app/llm.py` | OpenAI Chat Completions provider |
| `app/logging.py` | JSON structured logging |
| `start.sh` | Entrypoint: Alembic migrations → uvicorn |
| `alembic/` | Database migration scripts |

## Setup

### 1. Bitrix24 local application

Create a **local application** in Bitrix24 Developer section:
- Set redirect URI to `https://YOUR_PUBLIC_URL/b24/oauth/callback`
- Note `client_id` and `client_secret`

### 2. Bitrix24 inbound webhook

Create an **inbound webhook** (Developer resources → Inbound webhook):
- Scope: `im` (messaging)
- Note the webhook URL (e.g. `https://b24-xxx.bitrix24.ru/rest/1/secret/`)

### 3. Bitrix24 chat bot

Register a chat bot in the Bitrix24 UI (Developer → Chat Bots → Add):
- Set the bot's **Event Handler URL** to `https://YOUR_PUBLIC_URL/b24/imbot/events`
- Note the bot's **ID**, **CODE**, **CLIENT_ID**, and **NAME**

### 4. OpenAI API key

Get an OpenAI API key at https://platform.openai.com/api-keys.
Without it, the bot works in echo mode.

### 5. Environment

Copy `.env.example` → `.env` and fill in all values.

### 6. Run

```bash
docker compose up --build
```

App listens on `http://localhost:8000`.

### 7. OAuth install

Open `http://localhost:8000/b24/install` → returns `auth_url`.
Open that URL in a browser and approve the app.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/b24/imbot/events` | Bitrix bot event handler (AI or echo) |
| GET | `/catalog/stats` | Product catalog statistics |
| POST | `/catalog/scrape` | Trigger manual full scrape |
| GET | `/catalog/search?q=...` | Search product catalog |
| GET | `/b24/install` | Generate OAuth authorization URL |
| GET | `/b24/oauth/callback` | Handle OAuth redirect |

## Background tasks

- **Full scrape** — daily (configurable via `SCRAPER_FULL_INTERVAL_S`)
- **Price/quantity refresh** — hourly (configurable via `SCRAPER_PRICE_INTERVAL_S`)
- If the catalog is empty on startup, the scraper runs immediately

## AI Chat features

- Product search by name, description, characteristics
- Price and availability info with product URLs
- FAQ: delivery, payment, storage, hours
- Clarifying questions for ambiguous queries
- Analog suggestions when a product is out of stock
- Keyword "оператор" / "operator" → transfer to live agent
- Per-dialog conversation history (Redis, 24h TTL)

## Test curl

```bash
# Emulate a Bitrix bot event (JSON format)
curl -sS -X POST http://localhost:8000/b24/imbot/events \
  -H "Content-Type: application/json" \
  -d '{
    "data": {
      "PARAMS": {
        "DIALOG_ID": "123",
        "MESSAGE": "Какая икра есть в наличии?"
      }
    }
  }'

# Check catalog stats
curl -sS http://localhost:8000/catalog/stats | python3 -m json.tool

# Search products
curl -sS "http://localhost:8000/catalog/search?q=креветки&limit=5" | python3 -m json.tool
```