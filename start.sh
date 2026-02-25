#!/usr/bin/env bash
set -euo pipefail

echo "=== myryba start.sh ==="

# -------------------------------------------------------
# 1. Run Alembic migrations
# -------------------------------------------------------
echo "[1/2] Running Alembic migrations …"
alembic upgrade head
echo "       Migrations OK ✓"

# -------------------------------------------------------
# 2. Start FastAPI (uvicorn)
#    If DB is empty, the background scraper loop will
#    automatically scrape myryba.ru on startup.
# -------------------------------------------------------
echo "[2/2] Starting uvicorn …"
# --proxy-headers  : trust X-Forwarded-Proto / X-Forwarded-For from the reverse proxy
#                    so that Starlette's url_for() generates https:// URLs and sqladmin
#                    CSS/JS assets are not blocked as mixed content.
# --forwarded-allow-ips='*' : allow the header from any upstream (the proxy is the only
#                    client that can reach port 8000 inside Docker).
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips='*'
