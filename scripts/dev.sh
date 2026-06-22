#!/bin/bash
# ===================================================================
# One-command startup for the local development environment
# - Bring up docker infra (postgres/redis/localstack/adminer)
# - Run alembic migration
# - Start FastAPI (uvicorn --reload)
# ===================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

# Load env
if [ -f .env.local ]; then
    set -a
    source .env.local
    set +a
fi

echo "==> Starting infra (postgres/redis/localstack/adminer/opensearch)..."
docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres redis localstack adminer opensearch

echo "==> Waiting for postgres healthy..."
until docker compose -f docker-compose.dev.yml exec -T postgres pg_isready -U "${DATABASE_USERNAME}" -d "${DATABASE_NAME}" > /dev/null 2>&1; do
    sleep 1
done
echo "    postgres ready"

echo "==> Waiting for OpenSearch healthy (商品搜尋需要)..."
until curl -sf http://localhost:9200/_cluster/health > /dev/null 2>&1; do
    sleep 1
done
echo "    opensearch ready"

echo "==> Running alembic migrations..."
uv run alembic upgrade head || echo "    (alembic not configured yet, skipping)"

echo ""
echo "==> Services up:"
echo "    Postgres:    localhost:5434  (user: ${DATABASE_USERNAME})"
echo "    Redis:       localhost:6380"
echo "    LocalStack:  localhost:4567"
echo "    Adminer:     http://localhost:8081"
echo ""

# Reap zombies: if the last `make dev` didn't exit cleanly, the uvicorn reloader/worker may still hold the port
# (Errno 48 Address already in use). Before starting, clear whoever holds the port + leftover orphaned uvicorn processes.
DEV_PORT="${PORT:-8000}"
STALE_PIDS="$(lsof -nP -iTCP:"$DEV_PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
ORPHAN_PIDS="$(pgrep -f 'uvicorn recommender.main' 2>/dev/null || true)"
ALL_STALE="$(printf '%s\n%s\n' "$STALE_PIDS" "$ORPHAN_PIDS" | sort -u | grep -v '^$' || true)"
if [ -n "$ALL_STALE" ]; then
    echo "==> 清掉殘留 process（port $DEV_PORT / uvicorn 孤兒）: $(echo $ALL_STALE | tr '\n' ' ')"
    for pid in $ALL_STALE; do kill -9 "$pid" 2>/dev/null || true; done
    sleep 1
fi

echo "==> Starting FastAPI (Ctrl-C to stop)..."
uv run uvicorn recommender.main:app --host 0.0.0.0 --port "$DEV_PORT" --reload
