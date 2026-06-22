#!/bin/bash
# Fully reset the DB (delete the volume and rebuild)
set -euo pipefail

cd "$(dirname "$0")/../.."

echo "==> Stopping postgres..."
docker compose -f docker-compose.dev.yml stop postgres

echo "==> Removing postgres volume..."
docker compose -f docker-compose.dev.yml rm -f postgres
docker volume rm marketing-recommandation_postgres_data 2>/dev/null || true

echo "==> Restarting postgres..."
docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres

echo "==> Waiting healthy..."
sleep 5

echo "==> Running migrations..."
uv run alembic upgrade head

echo "==> ✅ DB reset complete"
