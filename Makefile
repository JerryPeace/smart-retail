# Marketing Recommendation POC - Make targets
#
# Usage:
#   make help          list all commands
#   make dev           one-command startup (infra + migration + FastAPI)
#   make search-setup  one-command build of the product-search vector index (load + embed)
#   make analyze MONTH=2026-04
#
# Design principles:
#   - The Makefile is the orchestration layer; the actual logic lives in scripts/* and service code
#   - Each target is independent and doesn't depend on others, so the user can compose them
#   - Dangerous actions (clearing volumes / restarting / spending money) are marked with a ⚠️ reminder

# Target index for product search (overridable: make search-setup SEARCH_INDEX=products_v6)
SEARCH_INDEX ?= products_v5_cohere

.PHONY: help \
        dev infra-up infra-down infra-clean infra-status \
        migrate api refresh-creds refresh-creds-mfa \
        search-setup search-load search-embed search-verify \
        analyze list-analyses narrative health \
        etl-april

# === Self-documenting help ===
help: ## List all commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ====================================================================
# One-command startup
# ====================================================================
dev: ## Bring up infra + migration + FastAPI (foreground, Ctrl-C to stop)
	./scripts/dev.sh

# ====================================================================
# Docker infra
# ====================================================================
infra-up: ## Bring up docker (postgres/redis/localstack/adminer/opensearch)
	docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres redis localstack adminer opensearch

infra-down: ## Stop docker (keep volumes, data preserved)
	docker compose -f docker-compose.dev.yml --env-file .env.local down

infra-clean: ## ⚠️  Stop docker and clear all volumes (DB data will be lost)
	docker compose -f docker-compose.dev.yml --env-file .env.local down -v

infra-status: ## Show the status of the 4 docker services
	@docker compose -f docker-compose.dev.yml --env-file .env.local ps

# ====================================================================
# Python / DB
# ====================================================================
migrate: ## Run alembic upgrade head (upgrade DB schema to latest)
	@set -a && . ./.env.local && set +a && uv run alembic upgrade head

api: ## Start FastAPI alone (foreground, --reload)
	@set -a && . ./.env.local && set +a && unset AWS_PROFILE && \
	  uv run uvicorn recommender.main:app --reload

# ====================================================================
# AWS
# ====================================================================
refresh-creds: ## Refresh the AWS lab temporary credentials (run once they expire after ~1hr, no MFA needed)
	./scripts/refresh-lab-creds.sh

refresh-creds-mfa: ## Use MFA to refresh the base session to 24h (no more MFA all day; must export AWS_MFA_ACCESS_KEY_ID/SECRET first)
	./scripts/refresh-session-token.sh

# ====================================================================
# Product search search_engine (vector index build — one-command vector flow for teammates)
# Prerequisites: OpenSearch up (make infra-up or make dev) + AWS lab credentials (make refresh-creds)
# Index name uses SEARCH_INDEX (default products_v5_cohere, aligned with the config default)
# ====================================================================
search-setup: ## 🔍 One-command build of the search index: load → embed (⚠️ embed uses Bedrock, costs money)
	@echo "==> [1/2] 載入商品到 $(SEARCH_INDEX) (free)..."
	@$(MAKE) search-load
	@echo "==> [2/2] Cohere v4 向量化 (⚠️ 真 Bedrock, ~\$$1, 15-30min)..."
	@$(MAKE) search-embed
	@echo "✅ 搜尋索引就緒. 起服務: make dev → 開 ui/search.html 或 make search-verify"

search-load: ## Create index + load 26k products into OpenSearch (no embedding, free, idempotent)
	OPENSEARCH_INDEX=$(SEARCH_INDEX) uv run python scripts/etl/load_products_os.py

search-embed: ## ⚠️  Cohere v4 vectorize all products (real Bedrock cost ~$1, resumable)
	OPENSEARCH_INDEX=$(SEARCH_INDEX) uv run python scripts/etl/embed_products_os.py

search-verify: ## Search smoke test (usage: make search-verify Q=手腳冰冷)
	@curl -sS -G http://localhost:8000/search \
	  --data-urlencode "q=$(or $(Q),氣炸鍋)" --data-urlencode "size=5" | \
	  jq '{query, route_label, applied_bm25_weight, results: [.results[] | {mart_name, score}]}'

# ====================================================================
# /analyses/sales API interaction
# ====================================================================
analyze: ## Trigger sales analysis for a given month (usage: make analyze MONTH=2026-04)
	@if [ -z "$(MONTH)" ]; then \
	  echo "❌ 缺 MONTH 參數. 用法: make analyze MONTH=2026-04"; exit 1; \
	fi
	@echo "📤 POST /analyses/sales (month=$(MONTH))"
	@curl -sS -X POST http://localhost:8000/analyses/sales \
	  -H 'Content-Type: application/json' \
	  -d '{"month":"$(MONTH)","force_rerun":true}' | jq
	@echo "⏳ Background task ~50s 後完成. 查結果: make narrative MONTH=$(MONTH)"

list-analyses: ## List the months already analyzed
	@curl -sS http://localhost:8000/analyses/sales | jq

narrative: ## Fetch the markdown brief for a given month (usage: make narrative MONTH=2026-04)
	@if [ -z "$(MONTH)" ]; then \
	  echo "❌ 缺 MONTH 參數. 用法: make narrative MONTH=2026-04"; exit 1; \
	fi
	@curl -sS http://localhost:8000/analyses/sales/$(MONTH)/artifacts/market-narrative

health: ## Health check (FastAPI + 4 docker containers)
	@echo "=== FastAPI /health/ready ==="
	@curl -sS http://localhost:8000/health/ready | jq || echo "  (FastAPI 沒起)"
	@echo ""
	@echo "=== Docker compose ps ==="
	@docker compose -f docker-compose.dev.yml --env-file .env.local ps

# ====================================================================
# ETL standalone (bypasses the API, runs scripts directly, for dev iteration)
# ====================================================================
etl-april: ## Run the 3 ETL scripts for April (writes to out/, bypasses the API)
	uv run python scripts/etl/aggregate_monthly.py
	uv run python scripts/etl/classify_dealers.py
	uv run python scripts/etl/cross_sell_gaps.py
