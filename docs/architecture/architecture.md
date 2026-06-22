# Architecture

Marketing Cleaner POC — 行銷推薦 pipeline 的架構設計與實作說明。

> 📐 **延伸架構文件**：搜尋子系統（hybrid product search，本文 §5.8 的完整展開——融合演算法、索引資料平面 v1–v5、失敗模式、含架構圖）見 [`search-architecture.md`](./search-architecture.md)。

## 1. 系統定位

**這是什麼**:把 本公司 業務資料(產品 × 客戶)透過 AI 分析,產出 *「給某客戶推薦某商品 + 理由 + 信心度」* 的結構化推薦報告,儲存到 DB 供下游消費(未來推到 HubSpot 給業務看)。

**POC 的範圍邊界**:

| ✅ 範圍內(已完成 / 進行中) | ❌ 範圍外(暫不做) |
|--------------------------|------------------|
| FastAPI + SQLModel 三層架構骨架 | SharePoint → S3 自動同步(AppFlow 暫緩) |
| LocalStack S3 模擬 + Postgres + Alembic | HubSpot Property / Note 同步(Phase 4) |
| LangChain + Bedrock Sonnet 4.5 真 LLM 整合 | 真實 ETL transformation 邏輯(等真資料) |
| Mock fallback(`ANALYZER_MOCK_MODE`)| Worker / queue(用 FastAPI BackgroundTasks 即可) |
| Prompt versioning + Evaluation 表結構 | 真實 prompt 內容、A/B 測試實作 |
| ~~`/analyses/sales` 月度跨經銷商分析 pipeline~~ (已下線，實作見 git history) | 個性化推薦(`/pipelines/run` 仍是 stub) |

## 2. 高層次架構

```
┌──────────────────────────────────────────────────────────────────┐
│                      [POC 邊界內]                                  │
│                                                                    │
│   HTTP request                                                     │
│        │                                                           │
│        ▼                                                           │
│   ┌──────────────────────────────────────────────────────────┐    │
│   │            FastAPI App (single process)                  │    │
│   │                                                          │    │
│   │   ┌──────────────────────────────────────────────────┐   │    │
│   │   │ Layer 1: api/  (Controllers — HTTP 邊界)         │   │    │
│   │   │  health · pipelines · recommendations            │   │    │
│   │   │  evaluations                                     │   │    │
│   │   └──────────────────┬───────────────────────────────┘   │    │
│   │                      ▼ Depends() DI                       │    │
│   │   ┌──────────────────────────────────────────────────┐   │    │
│   │   │ Layer 2: services/  (業務邏輯)                    │   │    │
│   │   │  S3Service · DatasetService · AgentService       │   │    │
│   │   │  RecommendationService · EvaluationService       │   │    │
│   │   │            └─→ PipelineService 編排              │   │    │
│   │   └──────────────────┬───────────────────────────────┘   │    │
│   │                      ▼                                    │    │
│   │   ┌──────────────────────────────────────────────────┐   │    │
│   │   │ Layer 3: repositories/  (DB CRUD)                 │   │    │
│   │   │  Job · Recommendation · PromptVariant · Eval     │   │    │
│   │   └──────────────────┬───────────────────────────────┘   │    │
│   │                      │                                    │    │
│   │   ┌──────────────────────────────────────────────────┐   │    │
│   │   │ search_engine 模組（top-level，同 app mount）       │   │    │
│   │   │  GET /search → service → repository              │   │    │
│   │   │  embed query（Cohere v4 / mock）→ msearch         │   │    │
│   │   │  k-NN + BM25 → min-max 融合 → SearchResponse DTO │   │    │
│   │   └──────────────────┬───────────────────────────────┘   │    │
│   │                      ▼                                    │    │
│   └──────────────────────┼───────────────────────────────────┘    │
│                          ▼                                         │
│     ┌─────────────────────────┐  ┌──────────────────────────┐     │
│     │   PostgreSQL 17         │  │  OpenSearch 2.19 (本地)  │     │
│     │   (SQLModel + Alembic)  │  │ products_v5_cohere k/BM25│     │
│     └─────────────────────────┘  └──────────────────────────┘     │
│                                                                    │
└─────────────────┬────────────────────────┬────────────────────────┘
                  │                        │
        ┌─────────▼────────┐    ┌──────────▼────────────┐
        │ LocalStack S3    │    │  AWS Bedrock          │
        │ (raw + cleaned)  │    │  Sonnet 4.5（LLM）    │
        │                  │    │  Cohere v4（embedding）│
        └──────────────────┘    └───────────────────────┘

[POC 邊界外 / 未來 Phase]
   AppFlow (SharePoint → S3) | HubSpot Sync | Production deployment
```

## 3. 技術棧

### 3.1 核心套件

| 類別 | 套件 | 版本 | 用途 |
|------|------|------|------|
| **Runtime** | Python | 3.14 | uv 管理 |
| **Web** | fastapi | 0.136+ | API server |
| | uvicorn | 0.34+ | ASGI server |
| **DB** | sqlmodel | 0.0.38+ | ORM + Pydantic 整合 |
| | asyncpg | 0.30+ | PostgreSQL async driver |
| | alembic | 1.14+ | Migration |
| | greenlet | 3.0+ | SQLAlchemy async 必需 |
| **AWS** | boto3 / aioboto3 | 最新 | AWS SDK |
| **LLM** | langchain | 1.2+ | LLM framework |
| | langchain-aws | 0.2+ | Bedrock 整合 |
| | langsmith | 0.8+ | Observability |
| **Search** | opensearch-py[async] | 最新 | AsyncOpenSearch client（`search_engine` 模組） |
| **ETL** | pandas | 3.0+ | DataFrame 操作 |
| **Validation** | pydantic | 2.10+ | DTO 驗證 |
| | pydantic-settings | 2.7+ | 環境變數 |

### 3.2 基礎設施(本地 dev)

| 服務 | image / 工具 | host port | 用途 |
|------|------------|-----------|------|
| Postgres | postgres:17 | 5434 | 資料庫 |
| Redis | redis:7-alpine | 6380 | 預留 (POC 不用 worker) |
| LocalStack | localstack/localstack:3.8 | 4567 | S3 模擬 |
| Adminer | adminer:latest | 8081 | DB GUI |
| OpenSearch | opensearchproject/opensearch:2.19.x | 9200 | hybrid search（k-NN faiss + BM25 smartcn；26,014 筆商品已向量化） |
| FastAPI | uv + python:3.14 | 8000 | 主應用 |

對齊 intellio.ai docker-compose conventions(只差 host port 錯開避免衝突)。

## 4. 三層架構與 DI

### 4.1 目錄結構

```
src/recommender/
├── main.py                  # FastAPI app entry + lifespan
├── config.py                # pydantic-settings 載 .env.local
├── db.py                    # async engine + session factory
├── deps.py                  # ⭐ DI providers 集中管理(唯一 wiring 點)
├── llm.py                   # @lru_cache Bedrock LLM builder(process 層級共用 client)
├── prompts.py               # 從 prompts/{module}/{version}.md 載入 + @cache
├── errors.py                # domain 例外 NotFoundError(與 HTTP 解耦)
├── timeutil.py              # 時區工具(naive UTC,對齊 TIMESTAMP WITHOUT TIME ZONE schema)
│
├── api/                     # 🟦 Layer 1: HTTP Controller
│   ├── health.py            #   /health/live, /health/ready
│   ├── pipelines.py         #   POST /pipelines/run (per-customer recommendation, stub)
│   ├── recommendations.py   #   GET /recommendations/{id}, /by-customer/{id}
│   └── evaluations.py       #   POST /evaluations/{rec_id}, GET /evaluations/{id}
│
├── chains/                  # LCEL chain factory (LLM 注入、與來源解耦)
│   ├── recommendation.py    #   build_recommendation_chain(llm) + RECOMMENDATION_PROMPT_VERSION
│   └── judge.py             #   build_judge_chain(llm) + JUDGE_PROMPT_VERSION
│
├── services/                # 🟩 Layer 2: 業務邏輯
│   ├── s3_service.py        #   讀寫 S3(LocalStack/AWS 自動切換)
│   ├── dataset_service.py   #   ETL: raw → cleaned dataset (stub,給 recommendation 用)
│   ├── agent_service.py     #   LangChain agent (含 prompt/guardrail/eval hooks)
│   ├── pipeline_service.py  #   編排 dataset → agent → save (per-customer)
│   ├── recommendation_service.py  # read service: get / list_by_customer
│   ├── evaluation_service.py      # LLM-as-judge + get / list_by_recommendation
│   └── promo_forecast_service.py  # ⚠️ 孤兒服務(見 §5.6)
│
├── repositories/            # 🟧 Layer 3: DB CRUD
│   ├── job_repo.py
│   ├── recommendation_repo.py
│   ├── prompt_variant_repo.py
│   └── evaluation_repo.py
│
├── models/                  # SQLModel ORM (DB tables)
│   ├── job.py               #   PipelineJob + JobStatus
│   ├── recommendation.py    #   Recommendation + HubSpotSyncStatus
│   ├── prompt_variant.py    #   PromptVariant
│   └── evaluation.py        #   Evaluation
│
└── schemas/                 # Pydantic DTOs / LLM contracts
    ├── pipeline.py          #   RunPipelineRequest, JobResponse
    ├── recommendation.py    #   RecommendationOutput (LLM 結構化輸出)
    ├── public.py            #   RecommendationPublic, EvaluationPublic (API DTO)
    ├── cleaning.py          #   CleaningReport
    └── evaluation.py        #   EvaluationOutput (judge LLM)
```

**⚠️ 獨立模組 — `search_engine`（與 recommender 平行的 top-level 模組）**：

codebase 預設「層優先」（相同職責的程式碼集中在同一層資料夾）。search 因基礎設施（OpenSearch + Cohere embedding）與核心（Postgres + Bedrock LLM）完全不同，已從 `recommender/search/` **升格為獨立 top-level 模組 `src/search_engine/`**，與 `src/recommender/` 平行。它是「同 app 內的獨立模組」：仍由 recommender 的 FastAPI app（`main.py`）mount 其 router、沿用 `recommender.config` 的同一個 Settings，但程式碼自含、依賴邊界清楚（誰依賴 OpenSearch 一目了然）。詳見 §5.8。

```
src/
├── recommender/             # 行銷推薦 pipeline（api/services/repositories 三層 + chains）
└── search_engine/           # 🟪 獨立模組（hybrid 商品搜尋，同 app mount、共用 config）
    ├── __init__.py
    ├── schemas.py           #   SearchResultItem / SearchResponse DTO
    ├── embeddings.py        #   Cohere v4 query embedder（boto3，input_type=search_query）+ MOCK_QUERY_VECTOR
    ├── client.py            #   @lru_cache get_opensearch_client + async close
    ├── repository.py        #   build_knn_body/build_bm25_body 純函式 + hybrid_msearch
    ├── fusion.py            #   min_max_score_fusion（上線）+ reciprocal_rank_fusion（測試）純函式
    ├── service.py           #   權重解析 → embed → msearch → min-max 融合 → DTO 編排
    └── router.py            #   GET /search（含可選 bm25_weight 覆寫）
```

### 4.2 各層職責原則

| 層 | 該做 | 不該做 |
|----|------|--------|
| **api/** | 接收 HTTP、call service、回 response | 寫業務邏輯、直接碰 DB |
| **services/** | 業務規則、外部呼叫(S3 / Bedrock)| 接 HTTP request、寫 SQL |
| **repositories/** | DB CRUD 操作 | 業務判斷、calling 其他 service |
| **models/** | SQLModel 表定義 | 包含業務方法 |
| **schemas/** | Pydantic 驗證 / 序列化 | 跟 DB 耦合 |
| **chains/** | LCEL chain 組裝（prompt + LLM）| 資料聚合、DB 操作 |

### 4.3 DI 機制(`deps.py`)

FastAPI 原生 `Depends()` 取代 NestJS Module / .NET DI container:

```python
# deps.py 是唯一管 wiring 的地方
SessionDep = Annotated[AsyncSession, Depends(get_session)]
JobRepoDep = Annotated[JobRepository, Depends(get_job_repo)]
PipelineServiceDep = Annotated[PipelineService, Depends(get_pipeline_service)]

# api/pipelines.py
@router.post("/run")
async def run_pipeline(body: RunPipelineRequest, service: PipelineServiceDep):
    ...
```

**原則**:
- 所有 service / repo 都是 concrete class(無 Protocol / ABC)
- 單一實作,測試靠 `unittest.mock` 或 `app.dependency_overrides`
- 真要 swap 實作時(例:本地 storage vs S3)再加 Protocol,5 分鐘事

### 4.4 根層模組（`llm.py` / `prompts.py` / `errors.py`）

這三個檔案是跨 service 共用的基礎設施，不屬於任何一層：

**`llm.py` — Bedrock LLM builder**
- `get_bedrock_llm(model, region, temperature, max_tokens, guardrail_items)` 加 `@lru_cache(maxsize=8)`
- 所有參數皆 hashable，作為 lru_cache key；`guardrail_items` 傳 `tuple[tuple[str, str], ...]` 而非 dict（dict 不可 hash）
- FastAPI DI 預設每 request 重建 service，`@lru_cache` 讓 Bedrock client 在 **process 層級共用**，避免重複建 boto3 session

**`prompts.py` — prompt 載入**
- `load_system_prompt(version, human_template) -> ChatPromptTemplate`
- 從 `prompts/{module}/{version}.md` 讀取系統指令，搭 human 觸發語組成 `ChatPromptTemplate`
- `@cache` 確保同一 version 只讀一次磁碟；**prompt 視為 immutable** —— 要改內容應發新 version（如 v1.1），不要原地修改 .md，以免 process 快取與檔案內容不符

**`errors.py` — domain 例外**
- `NotFoundError(Exception)` — 查無資源（recommendation / job / evaluation 等）
- service / repository 層拋，`main.py` 全域 exception handler 統一轉 HTTP 404
- service 不直接 raise HTTPException（service 可能被 background task / CLI / 測試呼叫）

## 5. 服務設計細節

### 5.1 S3Service

**職責**:讀寫 S3,純 I/O 不做業務轉換。

```python
class S3Service:
    async def get_object(bucket, key) -> bytes
    async def get_text(bucket, key, encoding) -> str
    async def put_object(bucket, key, body, content_type)
    async def put_text(bucket, key, text, content_type)
    async def list_objects(bucket, prefix) -> list[str]
    async def exists(bucket, key) -> bool
```

**LocalStack vs 真 AWS 自動切換**:由 `AWS_ENDPOINT_URL_S3` 環境變數決定。設了走 LocalStack,留空走真 AWS。

### 5.2 DatasetService(ETL,目前是 stub)

**職責**:從 S3 raw 讀原始資料(products + customers)→ 套 mapping/驗證 → 整理成 LLM 可用的 dataset → 寫到 S3 cleaned bucket。

**現狀**:`prepare()` method 是 stub,留好 pandas read_csv / mapper / 驗證 / put_text 範例骨架,等真實業務資料來填。

### 5.3 AgentService(LangChain agent)

```python
class AgentService:
    # Public
    async def analyze(customer_id, dataset_s3_key) -> RecommendationOutput
    async def trigger_evaluation(recommendation_id) -> None  # stub

    # Private
    def _guardrail_config() -> dict | None    # Guardrail (Bedrock 內建)
    def _mock_response() -> RecommendationOutput  # POC mock 模式
```

**Mock 模式**:`ANALYZER_MOCK_MODE=true` 回固定 fixture(POC 第一週用)。設 false 切到真 Bedrock。

**Bedrock 整合**:透過 `chains/recommendation.py` 的 `build_recommendation_chain(llm)` 組 LCEL chain；LLM 實例由 `llm.get_bedrock_llm(...)` 取得（process 層級快取）。

**結構化輸出**:chain 內用 `llm.with_structured_output(RecommendationOutput)`,Pydantic schema 自動翻譯成 JSON Schema 餵給 LLM,LLM 違反 schema 時 LangChain 自動 retry 帶錯誤訊息。

**Prompt 來源**:唯一來源是 `prompts/recommendation/v1.0.md`（由 `chains/recommendation.py` 的 `RECOMMENDATION_PROMPT_VERSION` 指向）。`PromptVariant` DB 表目前無 runtime 讀寫路徑（見 §6.4）。

### 5.4 PipelineService(編排)

```python
async def run(job_id):
    try:
        update_status('cleaning')
        cleaned_key, report = await dataset.prepare(...)

        update_status('analyzing')
        agent_output, variant_id = await agent.analyze(...)

        update_status('saving')
        rec = await rec_repo.create_from_agent_output(...)

        update_status('done', recommendation_id=rec.id)
    except Exception as e:
        update_status('failed', error=...)
        raise
```

**執行模式**:由 `POST /pipelines/run` 觸發,放進 FastAPI `BackgroundTasks` 在 response 送出後執行,**同 process 內非同步**。

**為什麼不用 worker (arq/celery)**:POC 規模單機 BackgroundTasks 夠用;jenkins 排程做 HubSpot 同步;升級到真 worker 是「單次 > 5 分鐘 + 多 instance scale + server 常重啟」訊號出現時。

### 5.5 SalesAnalysisService（已下線，實作見 git history）

`SalesAnalysisService`、`api/analyses.py`、`/analyses/sales/*` 端點已從 `src/` 移除（僅剩 pycache）。
月度跨經銷商市場分析的完整設計與實作說明可查 git history（commit 之前的 `src/recommender/services/sales_analysis_service.py`）。

### 5.6 RecommendationService / EvaluationService（read service 層）

**RecommendationService**（`services/recommendation_service.py`）:

```python
class RecommendationService:
    async def get(rec_id: int) -> RecommendationPublic          # 查無拋 NotFoundError
    async def list_by_customer(customer_id: str, limit: int = 20) -> list[RecommendationPublic]
```

**EvaluationService**（`services/evaluation_service.py`）:

```python
class EvaluationService:
    async def evaluate(recommendation_id: int) -> EvaluationPublic    # LLM-as-judge
    async def get(eval_id: int) -> EvaluationPublic
    async def list_by_recommendation(recommendation_id: int) -> list[EvaluationPublic]
```

兩個 service 均回傳 Pydantic DTO（`RecommendationPublic` / `EvaluationPublic`），不回傳 ORM 物件給 API 層。查無資源時拋 `NotFoundError`，由 `main.py` 全域 handler 轉 404。

**EvaluationService 的 judge chain**:使用 `chains/judge.py` 的 `build_judge_chain(llm)`，prompt 版本常數 `JUDGE_PROMPT_VERSION = "judge/v1.0"`。輸出 `{"parsed": EvaluationOutput, "raw": AIMessage}`，從 `raw` 抽取 token usage 寫入 DB。

### 5.7 PromoForecastService（孤兒服務，未接 API）

`services/promo_forecast_service.py`，約 451 行。

**功能**:月度專戶促銷預測，針對 33 家專戶業務課活躍經銷商做 R8 跨品類機會分析。純 deterministic ETL + reasoning chain，**不打 LLM**。

**孤兒狀態**（如實記載）:
- 目前**尚未接任何 API router**，無 HTTP 端點可呼叫
- 33 家專戶統編硬寫在 `promo_forecast_service.py:85` 的 `ZHUANHU_TAX_IDS` 常數（production 應接 HubSpotService 動態取得）
- 接 API / 統編外移屬於新功能接線，不在架構收斂範圍內；待另開 change 設計端點後接入

**資料來源**:月度 `104e 客戶別.xlsx`（`{N}月` sheet）+ 經濟部公示所營事業（透過 g0v 公司寶 API）。

### 5.8 search_engine 模組（`src/search_engine/`）

> 📌 **本節是高層摘要；搜尋子系統的完整、最新架構（融合演算法、索引資料平面 v1–v5、失敗模式、含架構圖）見 [`search-architecture.md`](./search-architecture.md)。** 兩文若有出入以 `search-architecture.md` 為準——融合演算法為上線的 **min-max score fusion**（加權 `w_bm25=0.2`，換 Cohere v4 後從 Titan 時代 0.7 重調）；向量化為 **Cohere Embed v4 / 1536 維**（索引 `products_v5_cohere`）。

**設計決策：升格為與 recommender 平行的獨立 top-level 模組**

既有 codebase 採「層優先」組織（`api/`、`services/`、`repositories/` 跨功能同層聚合）。search 因儲存基礎設施（OpenSearch + Cohere embedding）與核心（PostgreSQL + Bedrock LLM）完全不同，已從 `recommender/search/` **升格為 `src/search_engine/`**，與 `src/recommender/` 平行。圈成獨立模組才能讓「誰依賴 OpenSearch」的邊界清楚、易抽換或獨立部署。

**與既有架構共存規則（同 app 內的獨立模組，不是平行宇宙）**：
- 模組**內部**仍遵循 router → service → repository 三層職責（router 不碰 client、service 不回 raw hit、repository 純 I/O 無業務判斷）。
- **DI wiring 仍只在 recommender 的 `deps.py`**，`search_engine` 不自建 DI 入口；router 由 recommender 的 `main.py` mount。
- **沿用 `recommender.config` 的同一個 Settings**（`search_engine` import `recommender.config`，不另立 config）。
- 未預期錯誤（OpenSearch 連線失敗）直接往上飄，由 `main.py` 全域 Exception handler 轉 500，`search_engine` 不自拋 `HTTPException`。
- async OpenSearch client 生命週期接進 `main.py` 既有 lifespan（`startup` 建 client、`shutdown` 呼叫 `close_opensearch_client()`），不另起常駐進程。

**`GET /search` 資料流**：

```
GET /search?q=查詢字串&size=10
    ↓
[1] router.py — 驗參數（q 必填、size 1–100），呼叫 SearchServiceDep
    ↓
[2] service.py — _embed_query(q)
    mock_mode=True  → 回 MOCK_QUERY_VECTOR（1536 維固定單位向量，零 Bedrock 呼叫）
    mock_mode=False → Cohere v4 query embedder（cohere.embed-v4:0、ap-northeast-1、
                      input_type=search_query、output_dimension=1536、回傳 L2 正規化）
    ↓
[3] repository.py — hybrid_msearch(vector, query, candidate_k=2×size)
    msearch 一次 round-trip 並發兩路查詢：
      · k-NN query（faiss/hnsw/innerproduct）← 嵌入語意
      · BM25 match query（smartcn 中文斷詞）← 詞面匹配
    回兩組 raw hits（_id + _source + raw _score）
    ↓
[4] fusion.py — min_max_score_fusion(knn_scored, bm25_scored, w_bm25, w_knn)
    w_bm25 解析：手動 ?bm25_weight= ＞ 固定 settings.search_bm25_weight(0.2)
    pure Python：每路 raw _score 各自 per-query min-max 正規化後加權相加，
    fused = w_knn·norm(knn) + w_bm25·norm(bm25)，w_knn = 1 - w_bm25
    （reciprocal_rank_fusion 仍保留，但只供單元測試）
    ↓
[5] service.py — 取 top-size，_id join metadata → SearchResultItem DTO
    ↓
SearchResponse(query=原文, results=[...], applied_bm25_weight, route_label)
    查無結果回 results=[]、HTTP 200（搜尋沒中是正常業務結果，不是錯誤）
```

**模組各檔職責**：

| 檔案 | 一句話職責 |
|------|-----------|
| `schemas.py` | `SearchResultItem`（`mart_id` / `mart_name` / `score` / `brand?` / `price?` / `category?`）與 `SearchResponse` DTO |
| `embeddings.py` | `@lru_cache get_bedrock_embeddings(...)` 回 cached Cohere v4 query embedder（boto3 直呼，`input_type=search_query`、`output_dimension`、L2 正規化、`asyncio.to_thread` 非阻塞）；`MOCK_QUERY_VECTOR` 1536 維固定單位向量 |
| `client.py` | `@lru_cache get_opensearch_client()` 回 `AsyncOpenSearch` 單例；`close_opensearch_client()` 供 lifespan shutdown 呼叫 |
| `repository.py` | `build_knn_body` / `build_bm25_body` 純函式建 DSL body；`hybrid_msearch` 發 `msearch` 回兩組 raw hits |
| `fusion.py` | `min_max_score_fusion`（上線用，加權 min-max 正規化融合）＋ `reciprocal_rank_fusion`（保留，僅單元測試）純函式；零 I/O、可單元測試 |
| `service.py` | `SearchService.search(query, size, bm25_weight)` 編排 權重解析 → embed → msearch → min-max 融合 → DTO；mock 判斷在此（對齊 `AgentService` 模式） |
| `router.py` | `GET /search` 端點（含可選 `bm25_weight` 手動覆寫）；注入 `SearchServiceDep`（`deps.py` wiring）|

**不變量（query 與 doc 必須同模型、同維度、同 normalize）**：以 Cohere Embed v4 / `output_dimension=1536` + faiss/hnsw/**innerproduct** 嵌入全量商品（索引 `products_v5_cohere`）。Cohere 的 float embedding 非單位長，故 doc 端（`embed_products_os.py`）與 query 端（`embeddings.py`）**兩端都 L2 正規化**——`innerproduct` 等價 cosine 的前提是兩端皆單位向量；任一端不正規化就是兩向量活在不同空間、k-NN 分數靜默全錯。另：doc 端用 `input_type=search_document`、query 端用 `input_type=search_query`（Cohere 不對稱編碼，短 query↔長商品描述的檢索品質關鍵）。

**OpenSearch 不可達時的行為**：`get_opensearch_client()` 建構不發網路連線（lazy connect）。真正發 msearch 時若 OpenSearch 離線，`AsyncOpenSearch` 拋連線例外 → 往上飄 → `main.py` 全域 handler 轉 HTTP 500。`search_engine` 不做降級（無降級設計，POC 範疇）。

## 6. 資料模型

### 6.1 ER 概覽

```
PromptVariant (1) ──< (N) Recommendation (1) ──> (1) PipelineJob
                              │
                              └─< (N) Evaluation
```

### 6.2 PipelineJob

追蹤每次 pipeline 執行狀態 + ETL 統計。

| 欄位 | 型別 | 用途 |
|------|------|------|
| `id` | int (PK) | |
| `customer_id` | str (indexed) | 哪個客戶 |
| `brand`, `month` | str | input 維度 |
| `status` | enum | queued / cleaning / merging / analyzing / saving / evaluating / done / failed |
| `error` | str? | 失敗原因 |
| `recommendation_id` | int? (FK) | 完成後填 |
| `rows_input/output/failed` | int? | ETL 統計 |
| `cleaning_report` | JSON? | ETL 詳細報告 |
| `raw_keys` | JSON? | 從哪些 raw S3 key 來 |
| `cleaned_dataset_key` | str? | merger 產出位置 |
| `created_at`, `updated_at` | datetime | |

### 6.3 Recommendation(JSONB hybrid pattern)

LLM 產出的銷售建議。**hybrid 設計**:hot columns 抽出來建 index,完整 payload 進 JSONB。

| 欄位類型 | 欄位 | 用途 |
|---------|------|------|
| **Identity** | id, customer_id | |
| **Hot columns**(indexed) | customer_segment, confidence_score | 高頻查詢欄位 |
| **Cold JSONB** | payload | 完整 agent 輸出 (single source of truth) |
| **Schema versioning** | schema_version | 追蹤 payload 結構版本 |
| **LLM metadata** | model_id, input_tokens, output_tokens, latency_ms | cost / performance 追蹤 |
| **A/B testing** | prompt_variant_id (FK) | 哪個 prompt 產出 |
| **Audit** | generated_at, pipeline_job_id | 可追溯 |
| **HubSpot sync** | hubspot_sync_status (enum), contact_id, note_id, synced_at, sync_error, sync_retries | ⚠️ Phase 4 reserved（見下方說明） |

**為什麼 JSONB hybrid**:LLM 輸出 schema 必然演化(下週加欄位、下月升 schema_version),JSONB 讓 DB 零 migration 適應變化;同時 hot columns 提供查詢效能。

**⚠️ HubSpot 6 欄目前狀態：Phase 4 reserved**
- `hubspot_sync_status` / `hubspot_contact_id` / `hubspot_note_id` / `hubspot_synced_at` / `hubspot_sync_error` / `hubspot_sync_retries` 六欄為 Phase 4 HubSpot Sync 預留
- 目前唯一有寫入路徑的是 `hubspot_sync_status=pending`（新建 recommendation 時自動設定）；其餘 5 欄無 runtime 讀寫路徑
- 欄位保留（forward-only migration 原則）；Phase 4 接 HubSpot 時啟用完整 sync 流程

### 6.4 PromptVariant（dormant — A/B testing 基礎設施，目前未連通）

```
id, name, version, template, is_active, weight, notes, created_at
```

**用途**:DB-managed prompt registry。同 `name` 可有多個 `is_active=True` 的 variants 做 A/B,`weight` 控制流量分配。

**⚠️ 目前狀態：dormant（表 schema 已就緒，但目前無 runtime 讀寫路徑）**
- Runtime 唯一 prompt 來源是 `prompts/{module}/{version}.md`（由 `chains/` 的 `*_PROMPT_VERSION` 常數指向）
- `PromptVariantRepository` 存在但未被任何 service 呼叫（`deps.py` 的 `get_prompt_variant_repo` 保留作 dormant 基建）
- 表結構保留（forward-only migration 原則）；未來啟用 prompt A/B 時再接通讀寫路徑

### 6.5 Evaluation(LLM-as-judge)

```
id, recommendation_id (FK), judge_model_id,
relevance_score, specificity_score, actionability_score, overall_score (indexed),
judge_reasoning, judge_input_tokens, judge_output_tokens, evaluated_at (indexed)
```

**用途**:用 judge LLM(通常 Opus 4.7)對 generator(Sonnet 4.5)產出的 recommendation 評分,4 維度 + 自由文字理由。`overall_score` 加 index 方便排名 prompt variants。

**現狀**:表結構已建好,實際 judge prompt 等真實資料來設計。

## 7. 資料流(完整 happy path)

```
HTTP POST /pipelines/run
    body: { customer_id: "C006", brand: "3c", month: "2026-05" }
    ↓
[1] api/pipelines.py
    - body 用 RunPipelineRequest 驗證
    - 從 deps 拿 PipelineService
    - service.create_job() 建 PipelineJob (status=queued)
    - background_tasks.add_task(service.run, job.id)
    - return JobResponse  ← HTTP 200 OK 立刻回
    ↓
═══════ HTTP response 已發出 ════════
    ↓
[2] PipelineService.run() in BackgroundTask
    ↓
[3] dataset_service.prepare(...) [stub]
    - 應做:S3 raw 讀 → mapper → 驗證 → S3 cleaned 寫
    - 目前回 mock cleaned_key + 空 CleaningReport
    ↓
[4] agent_service.analyze(...)
    if mock_mode:
        return _mock_response()  ← 固定 fixture
    else:
        llm = ChatBedrockConverse(...)  ← lazy init
        structured_llm = llm.with_structured_output(RecommendationOutput)
        result = await structured_llm.ainvoke(prompt)
        return result, variant_id
    ↓
[5] recommendation_repo.create_from_agent_output(...)
    - Pydantic agent_output → SQLModel Recommendation
    - payload = output.model_dump(mode="json")  ← 進 JSONB
    - hot columns 抽出 (customer_segment, confidence_score)
    - INSERT INTO recommendation
    ↓
[6] job_repo.update_status('done', recommendation_id=rec.id)
    ↓
[後續 GET]
    GET /pipelines/{job_id}        → 看 status
    GET /recommendations/{rec_id}  → 看 LLM 推薦內容
    GET /recommendations/by-customer/{customer_id} → 該客戶歷史推薦
```

**端到端延遲**:Mock mode ~70ms;真 Bedrock ~5-15 秒。

### 7.2 Sales Analysis 資料流（已下線，實作見 git history）

`SalesAnalysisService` 與 `api/analyses.py` 已從 `src/` 移除。
完整資料流（月度跨經銷商 ETL pipeline + Bedrock narrative 產出）可查 git history。

## 8. AWS Bedrock 整合

### 8.1 認證

**現況**:lab role assume 後用 `aws configure export-credentials --profile lab --format env` 寫進 `.env.local`,FastAPI 啟動時 `set -a; source .env.local; set +a` 把暫時憑證匯到 process env,boto3 自動讀取。

**為什麼不用 `AWS_PROFILE=lab`**:Python boto3 跟 CLI 的 credential refresh 流程不同,設了 PROFILE 會觸發 MFA prompt,在無 stdin 的 process 內 raise EOFError。**直接用暫時憑證 env vars 避開**。

**重要工具**:`scripts/refresh-lab-creds.sh` — 自動把 lab profile 暫時憑證寫進 `.env.local`,過期(1-12 小時)就重跑。

### 8.2 Model selection

**設定**:`BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0`

**為什麼有 `us.` 前綴**:Sonnet 4.5 等新版 Claude **必須走 cross-region inference profile**,不能直接用 base model ID。`us.` 前綴讓 AWS 自動 routing 到 us-east-1 / us-east-2 / us-west-2 中當下最有空的 region。

### 8.3 Observability(Bedrock CloudWatch)

**Metrics namespace**:`AWS/Bedrock`,可用 dimension `By ModelId`。

**重要 metric**:Invocations、InvocationLatency、InputTokenCount、OutputTokenCount、InvocationClientErrors、InvocationServerErrors、EstimatedTPMQuotaUsage。

**雙記特性**:同一次呼叫同時記在 `us.anthropic.*`(inference profile)+ `anthropic.*`(base model)兩個 ModelId 下;**token 跟 latency metrics 只記在 `us.` 前綴**。

**delay**:CloudWatch 5-15 分鐘 propagation delay。

### 8.4 預留接口(待整合)

- **Guardrail**:`agent_service._guardrail_config()` 已留好 hook,等 AWS Console 建好 guardrail 填 `BEDROCK_GUARDRAIL_ID` 即可
- **Invocation Logging**:預設關閉,production 階段啟用送 S3
- **LangSmith tracing**:設 `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY` 環境變數,LangChain 自動 instrument

## 9. 設定與環境

### 9.1 環境變數(`.env.local`)

```bash
# Application
PORT=8000
ENVIRONMENT=dev

# Database
DATABASE_URL=postgresql+asyncpg://poc:poc@localhost:5434/marketing_cleaner

# Redis (預留)
REDIS_URL=redis://:redispoc@localhost:6380

# AWS / LocalStack
AWS_ENDPOINT_URL_S3=http://localhost:4567   # 設了走 LocalStack
AWS_REGION=us-east-1

# AWS Lab credentials (由 refresh-lab-creds.sh 自動寫入)
AWS_ACCESS_KEY_ID=ASIA...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...
AWS_CREDENTIAL_EXPIRATION=...

# S3 buckets
S3_RAW_BUCKET=raw-data
S3_CLEANED_BUCKET=cleaned-data
S3_ROOT_PREFIX=marketing-recommandation

# Bedrock
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
BEDROCK_REGION=us-east-1

# Feature flags
ANALYZER_MOCK_MODE=false   # true=mock fixture; false=真 Bedrock

# 預留(未啟用):
# LANGSMITH_TRACING / LANGSMITH_API_KEY
# BEDROCK_GUARDRAIL_ID / BEDROCK_GUARDRAIL_VERSION
# HUBSPOT_API_KEY
```

### 9.2 啟動順序

```bash
# 1. 啟動本地 infra(postgres / redis / localstack / adminer)
docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres redis localstack adminer

# 2. (可選)Refresh lab credentials
./scripts/refresh-lab-creds.sh

# 3. Source 暫時憑證到 shell + 啟動 FastAPI
set -a && source .env.local && set +a && unset AWS_PROFILE
uv run uvicorn recommender.main:app --reload
```

### 9.3 LocalStack S3 結構

**Raw zone**(原檔不動 + manifest):
```
s3://raw-data/
└── marketing-recommandation/                              ← S3_ROOT_PREFIX
    ├── products/                                    ← 商品 master
    │   ├── 3c/2026/05/products.csv
    │   ├── healthy/2026/05/products.csv
    │   ├── home-appliance/2026/05/products.csv
    │   └── daily-necessities/2026/05/products.csv
    ├── customers/                                    ← 客戶 master
    │   └── customers.csv
    └── sales/                                        ← 月度銷售類資料(新增)
        └── 2026/
            ├── 01/.keep ... 03/.keep                       ← 11 個空月份骨架
            ├── 04/                                          ← 4 月實際資料
            │   ├── 績效追蹤4月.xlsx                          ← sales 原檔不 rename
            │   ├── 手機平板資訊家電週邊(月銷售&同期).xlsx
            │   ├── 經銷-業績達成日報表(new)_zh-tw.xlsx
            │   └── _manifest.json                           ← logical→physical 對映
            └── 05/.keep ... 12/.keep
```

**Cleaned zone**(ETL + LLM 產出):
```
s3://cleaned-data/
└── marketing-recommandation/
    └── sales/                                         ← 對齊 raw zone 命名
        └── 2026/04/
            ├── region_category_summary.csv                  ← Tier 1 ETL #1
            ├── dealer_classification.csv                    ← Tier 1 ETL #2
            ├── cross_sell_gaps.csv                          ← Tier 1 ETL #3
            └── market_analysis.md                           ← Bedrock narrative
```

**設計原則**:
- **Raw immutable**:檔名照 sales 原樣保留(中文、括號、空格全 ok),修正版用 `04-rev2/` 獨立資料夾不覆蓋
- **Cleaned 用英文統一檔名**:給程式吃的,不用對齊原檔名
- **Manifest pattern**:logical name(`performance-tracking`)→ physical filename,跨月份檔名變動 ETL 不需改
- **`{YYYY}/{MM}/` 兩層分區**:對齊既有 `products/{category}/{YYYY}/{MM}/` convention,year-level lifecycle policy 友善

**Manifest 範本**:
```json
{
  "month": "2026-04",
  "uploaded_at": "2026-05-01T10:00:00+08:00",
  "uploaded_by": "sales-ops@example.com",
  "batch_status": "complete",
  "files": {
    "performance-tracking": "績效追蹤4月.xlsx",
    "monthly-sales": "手機平板資訊家電週邊(月銷售&同期).xlsx",
    "daily-performance": "經銷-業績達成日報表(new)_zh-tw.xlsx"
  }
}
```

**`scripts/localstack/init-buckets.sh`**:LocalStack 啟動時自動建 bucket + 把 `products/` `customers/` `sales/` 同步上去,排除 `~$*`(Office lock files)、`.DS_Store`、`.gitkeep`。

**Local source of truth**:`aws-s3/` 目錄結構 = S3 結構(1:1 mirror),sync 只是搬運。任何 team 成員 clone repo + 啟 LocalStack 就有一致環境。

### 9.4 測試（`tests/`）

```
tests/
├── conftest.py            # 強制 mock mode(env var + patch settings)、ASGI async client fixture
├── test_pipeline_e2e.py   # mock-mode 全流程 + 404 負路徑 —— 需要 dev Postgres
├── test_etl_units.py      # ETL 純函式單元測試 —— 無 DB / 無網路
├── test_chains.py         # chain 組裝 contract(fake LLM 注入) —— 無 DB / 零 Bedrock
└── test_guardrail.py      # B2 guardrail 設定實效驗證 —— 無 DB
```

分兩層跑:

| 層 | 指令 | 前置 |
|---|---|---|
| unit(ETL / chains / guardrail) | `uv run pytest tests/test_etl_units.py tests/test_chains.py tests/test_guardrail.py` | 無 |
| e2e(完整 pipeline) | `uv run pytest tests/test_pipeline_e2e.py` | `make infra-up` + `make migrate` |

關鍵約定:
- **永不打真 Bedrock**:conftest 在 import app 前設 `ANALYZER_MOCK_MODE=true`,並在 import 後直接 patch `settings.analyzer_mock_mode = True`(雙重防護 —— `main.py` 的 `load_dotenv(override=True)` 會用 `.env.local` 覆寫 env var)。
- **chain 測試不能用 `FakeListChatModel`**:兩條 chain 走 `with_structured_output`(底層 `bind_tools`),langchain-core 的 fake model 均未實作 `bind_tools`;改用自製 `FakeStructuredChatModel(GenericFakeChatModel)` stub `bind_tools` 餵帶 `tool_calls` 的 `AIMessage`。
- **e2e 用真 Postgres 不用 SQLite**:JSON 欄位與 datetime 語意在 SQLite 下會靜默失準。

## 10. Phase 計畫

| Phase | 範圍 | 狀態 |
|-------|------|------|
| **0** | Scaffolding(三層架構 + 4 services + 4 tables + Mock analyzer)| ✅ 完成 |
| **1** | 真 Bedrock 整合(authentication + Sonnet 4.5 + structured output)| ✅ 完成 |
| **1.5** | ETL 真實邏輯 | ✅ **完成(scope pivot)** |
| **1.6** | ~~Sales analysis 模組(`/analyses/sales`)+ Bedrock narrative~~ (已下線) | ✅ **完成後已移除** |
| **架構收斂** | 分層邊界修補 + 死碼清理 + chains/ 抽離 + 文件同步 | ✅ 完成 |
| **2（search）** | Hybrid search API（`src/search_engine/`）— Cohere v4 嵌入 + BM25+k-NN+min-max 融合 + `GET /search` endpoint | ✅ 完成（2026-06-13） |
| **2** | Prompt management 啟用(填 PromptVariant 表 + 寫第一版 prompt)| ⏸ 等 Phase 3 |
| **3** | Evaluation pipeline(LLM-as-judge / A/B 統計)| ⏸ 業務驗證後做 |
| **4** | SharePoint → S3 Sync 腳本(取代手動 seed) | ⏸ 業務驗證後做 |
| **5** | HubSpot Renderer + Sync(transform → Properties + Note)| ⏸ 業務驗證後做 |
| **6** | Production hardening(structlog / RequestID / retry / pre-commit / persistent analyses table)| ⏸ POC 結束才做 |

**Phase 1.5 scope pivot 說明**:原計畫(見 [data-governance.md](../plans/data-governance.md))是把 `DatasetService.prepare()` 從 stub 變成真 ETL 餵給個性化推薦。實際 session 中發現業務真正需要的是「**月度跨經銷商市場分析**」(不是 per-customer recommendation),所以建了 `SalesAnalysisService` 走完全不同的 pipeline。該模組後已在架構收斂時移除(僅留 git history)。`DatasetService.prepare()` 仍是 stub(per-customer 流程未啟動)。詳見 [data-governance.md §9 實際產出](../plans/data-governance.md#9-實際產出-outcome)。

## 11. 設計原則摘要

1. **Boundary validation**:HTTP edge 用 Pydantic 強制驗證,內部信任型別
2. **Single source of truth**:DB 存 JSONB(完整 LLM 輸出),不預渲染給 HubSpot 用
3. **Lazy on abstraction**:concrete class 直寫,有真實多實作需求才加 Protocol
4. **Schema-as-data**:同一份 Pydantic schema 同時用於 LLM contract / API DTO / DB 寫入驗證
5. **Mock mode 並行開發**:用 `ANALYZER_MOCK_MODE` 讓 ETL/DB 邏輯不被 Bedrock 權限阻塞
6. **POC 範圍紀律**:不做 worker / 不做 ABC / 不做 interface / 不做 abstract class — 真有需求 5 分鐘加得上

## 12. 重要參考文件

- `README.md` — setup 步驟與啟動指令
- `docker-compose.dev.yml` — 本地 infra 完整定義(對齊 intellio.ai conventions)
- `pyproject.toml` — 套件清單與版本約束
- `alembic/versions/*.py` — DB schema migration 歷史
- `scripts/refresh-lab-creds.sh` — AWS lab 憑證 refresh 工具
- `scripts/localstack/init-buckets.sh` — LocalStack S3 啟動初始化(含 sales/ 同步)
- `src/recommender/chains/` — LCEL chain factory（`build_recommendation_chain` / `build_judge_chain`）
- `src/recommender/services/promo_forecast_service.py` — 月度專戶促銷預測 service（451 行，孤兒服務，見 §5.7）
- ~~`src/recommender/services/sales_analysis_service.py`~~ — 已下線，見 git history
- ~~`src/recommender/api/analyses.py`~~ — 已下線，見 git history
