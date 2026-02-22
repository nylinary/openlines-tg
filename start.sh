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
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
