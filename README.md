# Marketing Cleaner POC

本公司 月度銷售資料 → ETL 彙整 → Bedrock LLM 市場分析 → 產出給業務主管的決策 brief。

> 完整架構說明見 [`docs/architecture/architecture.md`](./docs/architecture/architecture.md)

---

## 🚀 Quick Start (一鍵啟動)

```bash
# 第一次跑(setup):
cp .env.example .env.local       # 環境變數
uv sync                           # 安裝 Python 依賴(含 recommender + search_engine 兩個套件)
open -a OrbStack                  # 或啟動 Docker Desktop

# 之後每次:
make dev                          # 一鍵起 docker + DB migration + FastAPI
```

`make dev` 會依序:
1. 起 **5 個 docker 容器**(postgres / redis / localstack / adminer / **opensearch**)
2. 等 postgres + opensearch healthy
3. 跑 `alembic upgrade head`(DB schema 升級)
4. 起 FastAPI uvicorn(foreground,Ctrl-C 停;啟動前會自清殘留 uvicorn,避免 Errno 48)

跑完之後 6 個服務都在運作。**要用商品搜尋,還需建向量索引——見下方 [🔍 商品搜尋引擎](#-商品搜尋引擎hybrid-search-setup)。**

---

## 🔍 商品搜尋引擎(Hybrid Search) setup

`search_engine` 是與 `recommender` 平行的獨立模組,把本公司 26k 商品做 **hybrid 搜尋 = BM25(詞面)+ k-NN(Cohere Embed v4 語意向量)+ min-max 融合**。架構詳見 [`docs/architecture/search-architecture.md`](./docs/architecture/search-architecture.md)。

### 同事一鍵 onboarding(從零到能搜)

```bash
# 1. clone 後,26k 商品 seed 資料已在 repo 內(products/OpenSearch_Full_*.json,已入庫)
git clone <repo> && cd marketing-recommandation && uv sync

# 2. 刷 AWS lab 憑證(向量化要打 Bedrock Cohere v4,需要憑證)
make refresh-creds                # ~1hr 過期;或 make refresh-creds-mfa 一次撐 24h

# 3. 起服務(會自動把 OpenSearch 一起起來)
make dev                          # foreground;OpenSearch healthy 後 FastAPI 才起

# 4. (另開 terminal)一鍵建向量索引:load 26k → Cohere v4 全量嵌入
make search-setup                 # ⚠️ embed 走真 Bedrock,一次性 ~$1、約 15–30 分鐘、可續跑

# 5. 開搜尋測試 UI
open ui/search.html               # 純 HTML,打 localhost:8000/search
```

跑完 `make search-setup` 後,OpenSearch 裡會有 `products_v5_cohere` 索引(26,014 筆全嵌)。打開 `ui/search.html` 輸入「冬天手腳冰冷」「氣炸鍋」「久坐肩頸痠痛」就能看到結果(只顯示相關度 ≥0.26 的)。

### 重點與常見坑

| 項目 | 說明 |
|------|------|
| **向量模型** | Cohere Embed v4(`cohere.embed-v4:0`)/ 1536 維 / region `ap-northeast-1`。索引 `products_v5_cohere`,融合權重 `w_bm25=0.2`(config 可調)。 |
| **憑證** | app 的搜尋向量化走 `lab` profile **自動續期**(`aws_profile=lab`);`.env.local` 靜態憑證受 AWS role chaining 限制最長 1h。整天不想重輸 MFA → `make refresh-creds-mfa`(需先 `export AWS_MFA_ACCESS_KEY_ID/SECRET`)。 |
| **search 500** | 多半是憑證過期。`make refresh-creds` 後**重啟 `make dev`**(process 啟動時讀憑證)。 |
| **成本** | `make search-embed`(含在 search-setup)是 26k × Cohere v4 ≈ <$1 一次性。重跑冪等只補缺、不重複計費。 |
| **重建/換索引** | `make search-setup SEARCH_INDEX=products_v6` 可指定別的索引名。 |

---

## 📦 服務清單(Docker stack)

| 服務 | Port | 用途 | 怎麼看 |
|------|------|------|--------|
| **FastAPI** | 8000 | API server | http://localhost:8000/docs (Swagger UI) |
| **Postgres** | 5434 | 主 DB(PipelineJob / Recommendation 等表)| http://localhost:8081 (Adminer) |
| **Redis** | 6380 | POC 預留(目前未用) | `redis-cli -p 6380 -a redispoc PING` |
| **LocalStack** | 4567 | 模擬 AWS S3(raw / cleaned bucket)| `awslocal --endpoint-url=http://localhost:4567 s3 ls` |
| **Adminer** | 8081 | Postgres GUI | http://localhost:8081 (server: postgres / user: poc / pass: poc / db: marketing_cleaner) |
| **OpenSearch** | 9200 | 商品 hybrid 搜尋(k-NN + BM25)| `curl localhost:9200/_cat/indices` (找 `products_v5_cohere`) |

**Port 為什麼這些?** 跟 intellio.ai 公司既有 docker stack 錯開,可同時跑互不干擾(intellio.ai 用 5433 / 6379 / 4566)。

---

## 🛠 Make Commands(統一介面)

```bash
make help                         # 列出所有指令
```

### 啟動 / 停止

```bash
make dev                          # 一鍵啟動全部(infra + migration + FastAPI)
make infra-up                     # 只起 5 個 docker(含 opensearch)不起 FastAPI
make infra-status                 # 看 docker 服務狀態
make infra-down                   # 停 docker(保留 DB 資料)
make infra-clean                  # ⚠️ 停 docker + 清掉 volume(DB 資料消失!)
```

### 開發 / 操作

```bash
make migrate                      # 只跑 alembic upgrade(infra 要先起)
make api                          # 只起 FastAPI(不重起 docker)
make health                       # 健康檢查(FastAPI + docker 容器)
```

### 商品搜尋(向量索引)

```bash
make search-setup                 # 🔍 一鍵建索引:load 26k + Cohere v4 全量嵌入(⚠️ ~$1 Bedrock)
make search-load                  # 只建索引 + 載入商品(無 embedding,free)
make search-embed                 # ⚠️ 只跑 Cohere v4 向量化(真 Bedrock,可續跑)
make search-verify Q=手腳冰冷      # 搜尋 smoke 測試(curl /search)
```

### AWS

```bash
make refresh-creds                # 刷新 lab 暫時憑證(~1hr 過期就跑,免 MFA)
make refresh-creds-mfa            # 用 MFA 把基底 session 刷成 24h(需先 export AWS_MFA_ACCESS_KEY_ID/SECRET)
```

### 跑分析(端到端 demo)

```bash
make analyze MONTH=2026-04        # 觸發某月分析(背景跑 ~50s)
make list-analyses                # 列出已分析月份
make narrative MONTH=2026-04      # 拉 markdown brief
```

### ETL standalone(不走 API)

```bash
make etl-april                    # 跑 4 月 3 個 ETL script,結果寫 out/
```

---

## 🐳 Docker 服務啟動詳解

### 第一次:確認 Docker daemon 在跑

macOS 推薦用 **OrbStack**(輕量、快,取代 Docker Desktop):

```bash
brew install --cask orbstack       # 第一次安裝
open -a OrbStack                   # 啟動
```

確認 daemon 通:
```bash
docker ps                          # 沒報錯 = OK
```

### 啟動 5 個 infra 容器

```bash
make infra-up
# 等價於:
# docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres redis localstack adminer opensearch
```

第一次起會做 3 件事:
1. **拉 image**(postgres:17 / redis:7-alpine / localstack:3.8 / adminer:latest / opensearch:2.19.x)
2. **建 named volumes**(`postgres_data`、`redis_data`、`localstack_data`、`opensearch_data`)— 資料持久化
3. **LocalStack `ready.d` 自動執行 `scripts/localstack/init-buckets.sh`** — 建 raw-data / cleaned-data buckets + 把 `aws-s3/` 同步上去

### 確認容器都 healthy

```bash
make infra-status
```

預期看到:
```
NAME                          STATUS
marketing-poc-postgres        Up X seconds (healthy)
marketing-poc-redis           Up X seconds (healthy)
marketing-poc-localstack      Up X seconds (healthy)
marketing-poc-adminer         Up X seconds
marketing-poc-opensearch      Up X seconds (healthy)
```

如果 STATUS 是 `(starting)`,等 30 秒再看。如果是 `(unhealthy)` 或 `Restarting`,先 `make infra-down` 再 `make infra-up`。

### 起 FastAPI(infra 都 ready 後)

```bash
make api
```

或一氣呵成:`make dev`(包含 migration + FastAPI)。

### 停止

```bash
make infra-down                    # 停容器,保留 volume(下次 up 資料還在)
make infra-clean                   # ⚠️ 連 volume 一起清,DB 重置
```

### 常見錯誤

| 訊息 | 原因 | 解法 |
|------|------|------|
| `Cannot connect to the Docker daemon at unix:///...orbstack` | OrbStack 沒啟動 | `open -a OrbStack` |
| `port is already allocated` | 5434/6380/4567/8081 被佔 | `lsof -i :{port}` 找誰佔,或停掉 |
| `database "marketing_cleaner" does not exist` | 第一次起,init script 還沒跑完 | 等 30 秒,或 `make infra-down` 後再 up |
| FastAPI 401 / search 500 on Bedrock | lab 憑證過期(~1 小時) | `make refresh-creds` 後**重啟 `make dev`**(process 啟動時讀憑證);整天免重輸 → `make refresh-creds-mfa` |
| `make dev` 報 `Errno 48 Address already in use` | 殘留 uvicorn 佔住 8000 | dev.sh 已會自清;若仍卡 `lsof -ti:8000 \| xargs kill -9` |

---

## 🔥 跑一次完整端到端

```bash
# 1. 起服務
make dev    # 在另一個 terminal,因為 dev 是 foreground

# 2. (新 terminal) 觸發 4 月分析
make analyze MONTH=2026-04
# 等 ~50 秒(99% 是 Bedrock latency)

# 3. 看分析報告
make narrative MONTH=2026-04 | head -50

# 4. 或從 Swagger UI 點點看
open http://localhost:8000/docs
```

### POC 已內建 4 月資料

`aws-s3/sales/2026/04/` 已有 sales 4 月 xlsx + manifest,LocalStack 啟動時自動同步進 S3。所以一啟動就可以跑 `make analyze MONTH=2026-04`。

未來新月份(5 月)資料來時的處理流程見 [`docs/plans/data-governance.md` §9.7](./docs/plans/data-governance.md)。

---

## 🗂 專案結構

```
.
├── Makefile                                ⭐ 統一操作介面
├── README.md                               本檔
├── pyproject.toml / uv.lock / .python-version
├── Dockerfile                              multi-stage uv build
├── docker-compose.dev.yml                  本地 5 容器定義(含 opensearch)
├── .env.local                              環境變數(從 .env.example copy)
│
├── alembic/ + alembic.ini                  DB migration
│
├── scripts/
│   ├── dev.sh                              ← `make dev`(起 infra 含 opensearch + 自清殭屍 + FastAPI)
│   ├── refresh-lab-creds.sh                ← `make refresh-creds`(lab 憑證 ~1h,免 MFA)
│   ├── refresh-session-token.sh            ← `make refresh-creds-mfa`(MFA 把基底刷 24h)
│   ├── localstack/init-buckets.sh          LocalStack ready.d 自動建 bucket + sync fixture
│   ├── db/                                 DB reset / dump 工具
│   └── etl/                                ETL + 搜尋 CLI(load_products_os / embed_products_os / judge…)
│
├── src/                                    ⭐ 兩個 top-level 套件
│   ├── recommender/                        🟦 行銷推薦 pipeline(api/services/repositories 三層 + chains)
│   └── search_engine/                      🟪 商品 hybrid 搜尋(獨立模組,同 app mount、共用 recommender.config)
│       └─ router/service/repository/fusion/embeddings/client/schemas;三層職責見 architecture.md §5.8
│
├── ui/search.html                          純 HTML 搜尋測試 UI(打 localhost:8000/search)
├── products/OpenSearch_Full_*.json   ⭐ 26k 商品 seed(已入庫;make search-setup 用)
│
├── aws-s3/                                 ⭐ S3 source of truth(local mirror)
│   ├── products/{category}/{YYYY}/{MM}/products.csv
│   ├── customers/customers.csv
│   └── sales/{YYYY}/{MM}/             月度銷售檔(原檔不 rename + manifest)
│       └── 04/{xlsx files} + _manifest.json
│
├── out/                                    ETL 本地產出(gitignored)
└── docs/
    ├── architecture/architecture.md         ⭐ 主架構文件(讀這個!)
    └── plans/
        ├── README.md
        └── data-governance.md               Phase 1.5 計畫 + 實際 outcome
```

---

## 🔑 關鍵環境變數(`.env.local`)

| 變數 | 預設 | 說明 |
|------|------|------|
| `ANALYZER_MOCK_MODE` | `true` | true=回 fixture / **搜尋向量化也走 mock(回固定向量,結果無意義)**;要真搜尋設 `false` 打真 Cohere |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Sonnet 4.5 cross-region inference profile(注意 `us.` 前綴必要) |
| `BEDROCK_REGION` | `us-east-1` | |
| `AWS_ACCESS_KEY_ID` / `_SECRET_` / `_SESSION_TOKEN` | (refresh-creds.sh 寫入) | lab role 暫時憑證(~1 小時過期,AWS role chaining 上限) |
| `AWS_PROFILE` | `lab` | 搜尋向量化的 boto3 走此 profile **自動續期**(config 預設 `lab`,不必設) |
| **`BEDROCK_EMBED_MODEL_ID`** | `cohere.embed-v4:0` | 商品搜尋向量模型(Cohere Embed v4) |
| **`BEDROCK_EMBED_REGION`** / **`EMBED_DIMENSIONS`** | `ap-northeast-1` / `1536` | embedding region 與維度 |
| **`OPENSEARCH_INDEX`** / **`SEARCH_BM25_WEIGHT`** | `products_v5_cohere` / `0.2` | 搜尋索引名與融合權重 |
| `AWS_ENDPOINT_URL_S3` | `http://localhost:4567` | 設了走 LocalStack,留空走真 AWS |
| `S3_RAW_BUCKET` / `S3_CLEANED_BUCKET` | `raw-data` / `cleaned-data` | |
| `S3_ROOT_PREFIX` | `marketing-recommandation` | 所有 key 都在這 prefix 下 |
| `DATABASE_URL` | `postgresql+asyncpg://poc:poc@localhost:5434/marketing_cleaner` | |

---

## 📚 進一步閱讀

- [`docs/architecture/architecture.md`](./docs/architecture/architecture.md) — 完整架構(三層 + DI + 兩條 pipeline + S3 layout + Bedrock 整合)
- [`docs/architecture/search-architecture.md`](./docs/architecture/search-architecture.md) — 🔍 **搜尋子系統權威文件**(Cohere v4 + BM25 hybrid、min-max 融合、失敗模式、架構圖)
- [`docs/plans/data-governance.md`](./docs/plans/data-governance.md) — Phase 1.5 ETL 計畫 + 實際 outcome(§9 含技術債 + 5 月實作步驟)
- `~/.claude/projects/.../memory/MEMORY.md` — 4 條業務脈絡 fact(本公司 客戶 = 經銷商、月度節奏、ETL first 等)

---

## 🎯 Phase 狀態速覽

| Phase | 範圍 | 狀態 |
|-------|------|------|
| 0 | Scaffolding | ✅ |
| 1 | 真 Bedrock 整合 | ✅ |
| 1.5 | ETL 真實邏輯 | ✅ scope pivot,實作 sales analysis |
| 1.6 | `/analyses/sales` API + Bedrock narrative | ✅ |
| 2（search）| 🔍 商品 hybrid 搜尋(`search_engine` 模組:Cohere v4 + BM25 + min-max,`GET /search`)| ✅ |
| 2 | Prompt management | ⏸ |
| 3 | Evaluation pipeline(LLM-as-judge) | ⏸ |
| 4 | SharePoint → S3 自動 sync 腳本 | ⏸ |
| 5 | HubSpot Renderer + Sync | ⏸ |
| 6 | Production hardening | ⏸ |
