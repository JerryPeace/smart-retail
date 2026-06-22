# Marketing Recommendation POC - Make targets
#
# 用法:
#   make help          列出所有指令
#   make dev           一鍵啟動 (infra + migration + FastAPI)
#   make search-setup  一鍵建商品搜尋向量索引 (load + embed)
#   make analyze MONTH=2026-04
#
# 設計原則:
#   - Makefile 是 orchestration layer, 實際邏輯在 scripts/* 與 service code
#   - 每個 target 獨立, 不互相 depend, 讓 user 可組合
#   - 危險動作 (清 volume / 重啟 / 花錢) 標 ⚠️ 提醒

# 商品搜尋目標索引 (可覆寫: make search-setup SEARCH_INDEX=products_v6)
SEARCH_INDEX ?= products_v5_cohere

.PHONY: help \
        dev infra-up infra-down infra-clean infra-status \
        migrate api refresh-creds refresh-creds-mfa \
        search-setup search-load search-embed search-verify \
        analyze list-analyses narrative health \
        etl-april

# === Self-documenting help ===
help: ## 列出所有指令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ====================================================================
# 一鍵啟動
# ====================================================================
dev: ## 起 infra + migration + FastAPI (foreground, Ctrl-C 停)
	./scripts/dev.sh

# ====================================================================
# Docker infra
# ====================================================================
infra-up: ## 起 docker (postgres/redis/localstack/adminer/opensearch)
	docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres redis localstack adminer opensearch

infra-down: ## 停 docker (保留 volume, 資料留著)
	docker compose -f docker-compose.dev.yml --env-file .env.local down

infra-clean: ## ⚠️  停 docker 並清掉所有 volume (DB 資料會消失)
	docker compose -f docker-compose.dev.yml --env-file .env.local down -v

infra-status: ## 看 4 個 docker 服務狀態
	@docker compose -f docker-compose.dev.yml --env-file .env.local ps

# ====================================================================
# Python / DB
# ====================================================================
migrate: ## 跑 alembic upgrade head (DB schema 升到最新)
	@set -a && . ./.env.local && set +a && uv run alembic upgrade head

api: ## 起 FastAPI 單獨 (foreground, --reload)
	@set -a && . ./.env.local && set +a && unset AWS_PROFILE && \
	  uv run uvicorn recommender.main:app --reload

# ====================================================================
# AWS
# ====================================================================
refresh-creds: ## 刷新 AWS lab 暫時憑證 (~1hr 過期就要跑，免 MFA)
	./scripts/refresh-lab-creds.sh

refresh-creds-mfa: ## 用 MFA 把基底 session 刷成 24h (整天不必再 MFA；需先 export AWS_MFA_ACCESS_KEY_ID/SECRET)
	./scripts/refresh-session-token.sh

# ====================================================================
# 商品搜尋 search_engine (向量索引建置 — 同事一鍵跑向量流程)
# 前置: OpenSearch 已起 (make infra-up 或 make dev) + AWS lab 憑證 (make refresh-creds)
# 索引名走 SEARCH_INDEX (預設 products_v5_cohere, 對齊 config 預設)
# ====================================================================
search-setup: ## 🔍 一鍵建搜尋索引: load → embed (⚠️ embed 走 Bedrock 會花錢)
	@echo "==> [1/2] 載入商品到 $(SEARCH_INDEX) (free)..."
	@$(MAKE) search-load
	@echo "==> [2/2] Cohere v4 向量化 (⚠️ 真 Bedrock, ~\$$1, 15-30min)..."
	@$(MAKE) search-embed
	@echo "✅ 搜尋索引就緒. 起服務: make dev → 開 ui/search.html 或 make search-verify"

search-load: ## 建索引 + 載入 26k 商品到 OpenSearch (無 embedding, free, 冪等)
	OPENSEARCH_INDEX=$(SEARCH_INDEX) uv run python scripts/etl/load_products_os.py

search-embed: ## ⚠️  Cohere v4 向量化全量商品 (真 Bedrock 花費 ~$1, 可續跑)
	OPENSEARCH_INDEX=$(SEARCH_INDEX) uv run python scripts/etl/embed_products_os.py

search-verify: ## 搜尋 smoke 測試 (用法: make search-verify Q=手腳冰冷)
	@curl -sS -G http://localhost:8000/search \
	  --data-urlencode "q=$(or $(Q),氣炸鍋)" --data-urlencode "size=5" | \
	  jq '{query, route_label, applied_bm25_weight, results: [.results[] | {mart_name, score}]}'

# ====================================================================
# /analyses/sales API 互動
# ====================================================================
analyze: ## 觸發某月銷售分析 (用法: make analyze MONTH=2026-04)
	@if [ -z "$(MONTH)" ]; then \
	  echo "❌ 缺 MONTH 參數. 用法: make analyze MONTH=2026-04"; exit 1; \
	fi
	@echo "📤 POST /analyses/sales (month=$(MONTH))"
	@curl -sS -X POST http://localhost:8000/analyses/sales \
	  -H 'Content-Type: application/json' \
	  -d '{"month":"$(MONTH)","force_rerun":true}' | jq
	@echo "⏳ Background task ~50s 後完成. 查結果: make narrative MONTH=$(MONTH)"

list-analyses: ## 列出已分析月份
	@curl -sS http://localhost:8000/analyses/sales | jq

narrative: ## 拉某月 markdown brief (用法: make narrative MONTH=2026-04)
	@if [ -z "$(MONTH)" ]; then \
	  echo "❌ 缺 MONTH 參數. 用法: make narrative MONTH=2026-04"; exit 1; \
	fi
	@curl -sS http://localhost:8000/analyses/sales/$(MONTH)/artifacts/market-narrative

health: ## 健康檢查 (FastAPI + 4 docker 容器)
	@echo "=== FastAPI /health/ready ==="
	@curl -sS http://localhost:8000/health/ready | jq || echo "  (FastAPI 沒起)"
	@echo ""
	@echo "=== Docker compose ps ==="
	@docker compose -f docker-compose.dev.yml --env-file .env.local ps

# ====================================================================
# ETL standalone (不走 API, 直接跑 script, dev iteration 用)
# ====================================================================
etl-april: ## 跑 4 月 3 個 ETL script (寫 out/, 不走 API)
	uv run python scripts/etl/aggregate_monthly.py
	uv run python scripts/etl/classify_dealers.py
	uv run python scripts/etl/cross_sell_gaps.py
