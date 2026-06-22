# Architecture

Marketing Cleaner POC — architecture design and implementation notes for the marketing recommendation pipeline.

> 📐 **Extended architecture doc**: the search subsystem (hybrid product search, the full expansion of §5.8 in this doc — fusion algorithm, index data planes v1–v5, failure modes, with architecture diagrams) is in [`search-architecture.md`](./search-architecture.md).

## 1. System Positioning

**What this is**: take the company's business data (products × customers), run it through AI analysis, and produce *"recommend product X to customer Y + rationale + confidence"* structured recommendation reports, stored in the DB for downstream consumption (eventually pushed to HubSpot for the sales team to see).

**POC scope boundaries**:

| ✅ In scope (done / in progress) | ❌ Out of scope (not for now) |
|--------------------------|------------------|
| FastAPI + SQLModel three-layer architecture skeleton | SharePoint → S3 auto-sync (AppFlow deferred) |
| LocalStack S3 mock + Postgres + Alembic | HubSpot Property / Note sync (Phase 4) |
| LangChain + Bedrock Sonnet 4.5 real LLM integration | Real ETL transformation logic (waiting for real data) |
| Mock fallback (`ANALYZER_MOCK_MODE`)| Worker / queue (FastAPI BackgroundTasks is enough) |
| Prompt versioning + Evaluation table schema | Real prompt content, A/B testing implementation |
| ~~`/analyses/sales` monthly cross-dealer analysis pipeline~~ (decommissioned, see git history for implementation) | Personalized recommendation (`/pipelines/run` is still a stub) |

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      [Inside POC boundary]                         │
│                                                                    │
│   HTTP request                                                     │
│        │                                                           │
│        ▼                                                           │
│   ┌──────────────────────────────────────────────────────────┐    │
│   │            FastAPI App (single process)                  │    │
│   │                                                          │    │
│   │   ┌──────────────────────────────────────────────────┐   │    │
│   │   │ Layer 1: api/  (Controllers — HTTP edge)         │   │    │
│   │   │  health · pipelines · recommendations            │   │    │
│   │   │  evaluations                                     │   │    │
│   │   └──────────────────┬───────────────────────────────┘   │    │
│   │                      ▼ Depends() DI                       │    │
│   │   ┌──────────────────────────────────────────────────┐   │    │
│   │   │ Layer 2: services/  (business logic)             │   │    │
│   │   │  S3Service · DatasetService · AgentService       │   │    │
│   │   │  RecommendationService · EvaluationService       │   │    │
│   │   │            └─→ PipelineService orchestration     │   │    │
│   │   └──────────────────┬───────────────────────────────┘   │    │
│   │                      ▼                                    │    │
│   │   ┌──────────────────────────────────────────────────┐   │    │
│   │   │ Layer 3: repositories/  (DB CRUD)                 │   │    │
│   │   │  Job · Recommendation · PromptVariant · Eval     │   │    │
│   │   └──────────────────┬───────────────────────────────┘   │    │
│   │                      │                                    │    │
│   │   ┌──────────────────────────────────────────────────┐   │    │
│   │   │ search_engine module (top-level, mounted on app)  │   │    │
│   │   │  GET /search → service → repository              │   │    │
│   │   │  embed query (Cohere v4 / mock) → msearch         │   │    │
│   │   │  k-NN + BM25 → min-max fusion → SearchResponse DTO │   │    │
│   │   └──────────────────┬───────────────────────────────┘   │    │
│   │                      ▼                                    │    │
│   └──────────────────────┼───────────────────────────────────┘    │
│                          ▼                                         │
│     ┌─────────────────────────┐  ┌──────────────────────────┐     │
│     │   PostgreSQL 17         │  │  OpenSearch 2.19 (local) │     │
│     │   (SQLModel + Alembic)  │  │ products_v5_cohere k/BM25│     │
│     └─────────────────────────┘  └──────────────────────────┘     │
│                                                                    │
└─────────────────┬────────────────────────┬────────────────────────┘
                  │                        │
        ┌─────────▼────────┐    ┌──────────▼────────────┐
        │ LocalStack S3    │    │  AWS Bedrock          │
        │ (raw + cleaned)  │    │  Sonnet 4.5 (LLM)     │
        │                  │    │  Cohere v4 (embedding)│
        └──────────────────┘    └───────────────────────┘

[Outside POC boundary / future Phases]
   AppFlow (SharePoint → S3) | HubSpot Sync | Production deployment
```

## 3. Tech Stack

### 3.1 Core Packages

| Category | Package | Version | Purpose |
|------|------|------|------|
| **Runtime** | Python | 3.14 | managed by uv |
| **Web** | fastapi | 0.136+ | API server |
| | uvicorn | 0.34+ | ASGI server |
| **DB** | sqlmodel | 0.0.38+ | ORM + Pydantic integration |
| | asyncpg | 0.30+ | PostgreSQL async driver |
| | alembic | 1.14+ | Migration |
| | greenlet | 3.0+ | required by SQLAlchemy async |
| **AWS** | boto3 / aioboto3 | latest | AWS SDK |
| **LLM** | langchain | 1.2+ | LLM framework |
| | langchain-aws | 0.2+ | Bedrock integration |
| | langsmith | 0.8+ | Observability |
| **Search** | opensearch-py[async] | latest | AsyncOpenSearch client (`search_engine` module) |
| **ETL** | pandas | 3.0+ | DataFrame operations |
| **Validation** | pydantic | 2.10+ | DTO validation |
| | pydantic-settings | 2.7+ | environment variables |

### 3.2 Infrastructure (local dev)

| Service | image / tool | host port | Purpose |
|------|------------|-----------|------|
| Postgres | postgres:17 | 5434 | database |
| Redis | redis:7-alpine | 6380 | reserved (POC has no worker) |
| LocalStack | localstack/localstack:3.8 | 4567 | S3 mock |
| Adminer | adminer:latest | 8081 | DB GUI |
| OpenSearch | opensearchproject/opensearch:2.19.x | 9200 | hybrid search (k-NN faiss + BM25 smartcn; 26,014 products vectorized) |
| FastAPI | uv + python:3.14 | 8000 | main application |

Aligned with intellio.ai docker-compose conventions (the only difference is staggered host ports to avoid conflicts).

## 4. Three-Layer Architecture and DI

### 4.1 Directory Structure

```
src/recommender/
├── main.py                  # FastAPI app entry + lifespan
├── config.py                # pydantic-settings loads .env.local
├── db.py                    # async engine + session factory
├── deps.py                  # ⭐ DI providers centralized (the sole wiring point)
├── llm.py                   # @lru_cache Bedrock LLM builder (process-level shared client)
├── prompts.py               # loads from prompts/{module}/{version}.md + @cache
├── errors.py                # domain exception NotFoundError (decoupled from HTTP)
├── timeutil.py              # timezone util (naive UTC, aligned with TIMESTAMP WITHOUT TIME ZONE schema)
│
├── api/                     # 🟦 Layer 1: HTTP Controller
│   ├── health.py            #   /health/live, /health/ready
│   ├── pipelines.py         #   POST /pipelines/run (per-customer recommendation, stub)
│   ├── recommendations.py   #   GET /recommendations/{id}, /by-customer/{id}
│   └── evaluations.py       #   POST /evaluations/{rec_id}, GET /evaluations/{id}
│
├── chains/                  # LCEL chain factory (LLM injected, decoupled from source)
│   ├── recommendation.py    #   build_recommendation_chain(llm) + RECOMMENDATION_PROMPT_VERSION
│   └── judge.py             #   build_judge_chain(llm) + JUDGE_PROMPT_VERSION
│
├── services/                # 🟩 Layer 2: business logic
│   ├── s3_service.py        #   read/write S3 (LocalStack/AWS auto-switch)
│   ├── dataset_service.py   #   ETL: raw → cleaned dataset (stub, for recommendation)
│   ├── agent_service.py     #   LangChain agent (incl. prompt/guardrail/eval hooks)
│   ├── pipeline_service.py  #   orchestrate dataset → agent → save (per-customer)
│   ├── recommendation_service.py  # read service: get / list_by_customer
│   ├── evaluation_service.py      # LLM-as-judge + get / list_by_recommendation
│   └── promo_forecast_service.py  # ⚠️ orphan service (see §5.6)
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
    ├── recommendation.py    #   RecommendationOutput (LLM structured output)
    ├── public.py            #   RecommendationPublic, EvaluationPublic (API DTO)
    ├── cleaning.py          #   CleaningReport
    └── evaluation.py        #   EvaluationOutput (judge LLM)
```

**⚠️ Standalone module — `search_engine` (a top-level module parallel to recommender)**:

The codebase defaults to "layer-first" organization (code with the same responsibility is grouped in the same layer folder). Because search's infrastructure (OpenSearch + Cohere embedding) is completely different from the core (Postgres + Bedrock LLM), it has been **promoted from `recommender/search/` to a standalone top-level module `src/search_engine/`**, parallel to `src/recommender/`. It is a "standalone module within the same app": its router is still mounted by the recommender's FastAPI app (`main.py`) and it reuses the same Settings from `recommender.config`, but the code is self-contained with clear dependency boundaries (who depends on OpenSearch is obvious at a glance). See §5.8 for details.

```
src/
├── recommender/             # marketing recommendation pipeline (api/services/repositories three layers + chains)
└── search_engine/           # 🟪 standalone module (hybrid product search, mounted on same app, shared config)
    ├── __init__.py
    ├── schemas.py           #   SearchResultItem / SearchResponse DTO
    ├── embeddings.py        #   Cohere v4 query embedder (boto3, input_type=search_query) + MOCK_QUERY_VECTOR
    ├── client.py            #   @lru_cache get_opensearch_client + async close
    ├── repository.py        #   build_knn_body/build_bm25_body pure functions + hybrid_msearch
    ├── fusion.py            #   min_max_score_fusion (production) + reciprocal_rank_fusion (tests) pure functions
    ├── service.py           #   weight resolution → embed → msearch → min-max fusion → DTO orchestration
    └── router.py            #   GET /search (with optional bm25_weight override)
```

### 4.2 Per-Layer Responsibility Principles

| Layer | Should do | Should not do |
|----|------|--------|
| **api/** | receive HTTP, call service, return response | write business logic, touch DB directly |
| **services/** | business rules, external calls (S3 / Bedrock)| handle HTTP requests, write SQL |
| **repositories/** | DB CRUD operations | business decisions, calling other services |
| **models/** | SQLModel table definitions | contain business methods |
| **schemas/** | Pydantic validation / serialization | coupling to the DB |
| **chains/** | LCEL chain assembly (prompt + LLM)| data aggregation, DB operations |

### 4.3 DI Mechanism (`deps.py`)

FastAPI's native `Depends()` replaces NestJS Module / .NET DI container:

```python
# deps.py is the only place that manages wiring
SessionDep = Annotated[AsyncSession, Depends(get_session)]
JobRepoDep = Annotated[JobRepository, Depends(get_job_repo)]
PipelineServiceDep = Annotated[PipelineService, Depends(get_pipeline_service)]

# api/pipelines.py
@router.post("/run")
async def run_pipeline(body: RunPipelineRequest, service: PipelineServiceDep):
    ...
```

**Principles**:
- All services / repos are concrete classes (no Protocol / ABC)
- Single implementation; testing relies on `unittest.mock` or `app.dependency_overrides`
- Add a Protocol only when you genuinely need to swap implementations (e.g. local storage vs S3) — a 5-minute job

### 4.4 Root-Level Modules (`llm.py` / `prompts.py` / `errors.py`)

These three files are cross-service shared infrastructure and do not belong to any single layer:

**`llm.py` — Bedrock LLM builder**
- `get_bedrock_llm(model, region, temperature, max_tokens, guardrail_items)` decorated with `@lru_cache(maxsize=8)`
- All parameters are hashable and serve as the lru_cache key; `guardrail_items` is passed as `tuple[tuple[str, str], ...]` rather than a dict (dicts are not hashable)
- FastAPI DI rebuilds the service per request by default; `@lru_cache` lets the Bedrock client be **shared at the process level**, avoiding rebuilding the boto3 session repeatedly

**`prompts.py` — prompt loading**
- `load_system_prompt(version, human_template) -> ChatPromptTemplate`
- Reads system instructions from `prompts/{module}/{version}.md` and combines them with the human trigger phrase into a `ChatPromptTemplate`
- `@cache` ensures each version is read from disk only once; **prompts are treated as immutable** — to change content you should release a new version (e.g. v1.1), do not edit the .md in place, otherwise the process cache will diverge from the file content

**`errors.py` — domain exceptions**
- `NotFoundError(Exception)` — resource not found (recommendation / job / evaluation, etc.)
- Raised at the service / repository layer; `main.py`'s global exception handler uniformly converts it to HTTP 404
- The service does not raise HTTPException directly (the service may be called by a background task / CLI / test)

## 5. Service Design Details

### 5.1 S3Service

**Responsibility**: read/write S3, pure I/O with no business transformation.

```python
class S3Service:
    async def get_object(bucket, key) -> bytes
    async def get_text(bucket, key, encoding) -> str
    async def put_object(bucket, key, body, content_type)
    async def put_text(bucket, key, text, content_type)
    async def list_objects(bucket, prefix) -> list[str]
    async def exists(bucket, key) -> bool
```

**LocalStack vs real AWS auto-switching**: determined by the `AWS_ENDPOINT_URL_S3` environment variable. Set it to use LocalStack, leave it empty to use real AWS.

### 5.2 DatasetService (ETL, currently a stub)

**Responsibility**: read raw data from S3 raw (products + customers) → apply mapping/validation → assemble into a dataset usable by the LLM → write to the S3 cleaned bucket.

**Current state**: the `prepare()` method is a stub, leaving a skeleton with pandas read_csv / mapper / validation / put_text examples, waiting to be filled in once real business data arrives.

### 5.3 AgentService (LangChain agent)

```python
class AgentService:
    # Public
    async def analyze(customer_id, dataset_s3_key) -> RecommendationOutput
    async def trigger_evaluation(recommendation_id) -> None  # stub

    # Private
    def _guardrail_config() -> dict | None    # Guardrail (Bedrock built-in)
    def _mock_response() -> RecommendationOutput  # POC mock mode
```

**Mock mode**: `ANALYZER_MOCK_MODE=true` returns a fixed fixture (used in the first week of the POC). Set false to switch to real Bedrock.

**Bedrock integration**: assembles the LCEL chain via `build_recommendation_chain(llm)` in `chains/recommendation.py`; the LLM instance is obtained from `llm.get_bedrock_llm(...)` (process-level cache).

**Structured output**: the chain uses `llm.with_structured_output(RecommendationOutput)`; the Pydantic schema is automatically translated into JSON Schema and fed to the LLM, and when the LLM violates the schema LangChain automatically retries with the error message attached.

**Prompt source**: the sole source is `prompts/recommendation/v1.0.md` (pointed to by `RECOMMENDATION_PROMPT_VERSION` in `chains/recommendation.py`). The `PromptVariant` DB table currently has no runtime read/write path (see §6.4).

### 5.4 PipelineService (orchestration)

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

**Execution model**: triggered by `POST /pipelines/run`, placed into FastAPI `BackgroundTasks` to run after the response is sent, **asynchronously within the same process**.

**Why no worker (arq/celery)**: at POC scale a single-machine BackgroundTasks is sufficient; jenkins scheduling handles HubSpot sync; upgrading to a real worker is signaled when "a single run takes > 5 minutes + multi-instance scale + frequent server restarts" appears.

### 5.5 SalesAnalysisService (decommissioned, see git history for implementation)

`SalesAnalysisService`, `api/analyses.py`, and the `/analyses/sales/*` endpoints have been removed from `src/` (only pycache remains).
The full design and implementation notes for the monthly cross-dealer market analysis can be found in git history (the `src/recommender/services/sales_analysis_service.py` that existed before the commit).

### 5.6 RecommendationService / EvaluationService (read service layer)

**RecommendationService** (`services/recommendation_service.py`):

```python
class RecommendationService:
    async def get(rec_id: int) -> RecommendationPublic          # raises NotFoundError if not found
    async def list_by_customer(customer_id: str, limit: int = 20) -> list[RecommendationPublic]
```

**EvaluationService** (`services/evaluation_service.py`):

```python
class EvaluationService:
    async def evaluate(recommendation_id: int) -> EvaluationPublic    # LLM-as-judge
    async def get(eval_id: int) -> EvaluationPublic
    async def list_by_recommendation(recommendation_id: int) -> list[EvaluationPublic]
```

Both services return Pydantic DTOs (`RecommendationPublic` / `EvaluationPublic`) and do not return ORM objects to the API layer. When a resource is not found they raise `NotFoundError`, which the global handler in `main.py` converts to 404.

**EvaluationService's judge chain**: uses `build_judge_chain(llm)` from `chains/judge.py`, with the prompt version constant `JUDGE_PROMPT_VERSION = "judge/v1.0"`. It outputs `{"parsed": EvaluationOutput, "raw": AIMessage}` and extracts token usage from `raw` to write to the DB.

### 5.7 PromoForecastService (orphan service, not wired to any API)

`services/promo_forecast_service.py`, about 451 lines.

**Function**: monthly key-account promotion forecasting, doing R8 cross-category opportunity analysis for the 33 active dealers under the key-account sales section. Pure deterministic ETL + reasoning chain, **does not call the LLM**.

**Orphan status** (recorded as-is):
- Currently **not wired to any API router**; there is no HTTP endpoint to call it
- The 33 key-account tax IDs are hard-coded in the `ZHUANHU_TAX_IDS` constant at `promo_forecast_service.py:85` (production should fetch them dynamically via HubSpotService)
- Wiring to an API / externalizing the tax IDs is new-feature plumbing, outside the scope of architecture convergence; it will be wired in after a separate change designs the endpoint

**Data sources**: the monthly `104e 客戶別.xlsx` (`{N}月` sheet) + the Ministry of Economic Affairs registered-business disclosure (via the g0v company-info API).

### 5.8 search_engine module (`src/search_engine/`)

> 📌 **This section is a high-level summary; the complete, up-to-date architecture of the search subsystem (fusion algorithm, index data planes v1–v5, failure modes, with architecture diagrams) is in [`search-architecture.md`](./search-architecture.md).** If the two docs disagree, `search-architecture.md` is authoritative — the production fusion algorithm is **min-max score fusion** (weighted `w_bm25=0.2`, re-tuned from the Titan-era 0.7 after switching to Cohere v4); vectorization is **Cohere Embed v4 / 1536 dimensions** (index `products_v5_cohere`).

**Design decision: promote it to a standalone top-level module parallel to recommender**

The existing codebase uses "layer-first" organization (`api/`, `services/`, `repositories/` aggregate cross-feature code in the same layer). Because search's storage infrastructure (OpenSearch + Cohere embedding) is completely different from the core (PostgreSQL + Bedrock LLM), it has been **promoted from `recommender/search/` to `src/search_engine/`**, parallel to `src/recommender/`. Walling it off as a standalone module makes the "who depends on OpenSearch" boundary clear, easy to swap out or deploy independently.

**Coexistence rules with the existing architecture (a standalone module within the same app, not a parallel universe)**:
- **Inside** the module it still follows the router → service → repository three-layer responsibilities (the router doesn't touch the client, the service doesn't return raw hits, the repository is pure I/O with no business decisions).
- **DI wiring still lives only in the recommender's `deps.py`**; `search_engine` does not build its own DI entry point; the router is mounted by the recommender's `main.py`.
- **It reuses the same Settings from `recommender.config`** (`search_engine` imports `recommender.config` rather than defining its own config).
- Unexpected errors (OpenSearch connection failure) propagate upward and are converted to 500 by the global Exception handler in `main.py`; `search_engine` does not raise `HTTPException` itself.
- The async OpenSearch client lifecycle is plugged into the existing lifespan in `main.py` (build the client on `startup`, call `close_opensearch_client()` on `shutdown`), rather than starting a separate long-running process.

**`GET /search` data flow**:

```
GET /search?q=query string&size=10
    ↓
[1] router.py — validate params (q required, size 1–100), call SearchServiceDep
    ↓
[2] service.py — _embed_query(q)
    mock_mode=True  → return MOCK_QUERY_VECTOR (1536-dim fixed unit vector, zero Bedrock calls)
    mock_mode=False → Cohere v4 query embedder (cohere.embed-v4:0, ap-northeast-1,
                      input_type=search_query, output_dimension=1536, returns L2 normalized)
    ↓
[3] repository.py — hybrid_msearch(vector, query, candidate_k=2×size)
    msearch runs two queries concurrently in one round-trip:
      · k-NN query (faiss/hnsw/innerproduct) ← embedded semantics
      · BM25 match query (smartcn Chinese tokenization) ← lexical match
    returns two sets of raw hits (_id + _source + raw _score)
    ↓
[4] fusion.py — min_max_score_fusion(knn_scored, bm25_scored, w_bm25, w_knn)
    w_bm25 resolution: manual ?bm25_weight= > fixed settings.search_bm25_weight(0.2)
    pure Python: each side's raw _score is per-query min-max normalized then weighted-summed,
    fused = w_knn·norm(knn) + w_bm25·norm(bm25), w_knn = 1 - w_bm25
    (reciprocal_rank_fusion is retained, but only for unit tests)
    ↓
[5] service.py — take top-size, _id join metadata → SearchResultItem DTO
    ↓
SearchResponse(query=original text, results=[...], applied_bm25_weight, route_label)
    no results returns results=[], HTTP 200 (no match is a normal business result, not an error)
```

**Per-file responsibilities of the module**:

| File | One-line responsibility |
|------|-----------|
| `schemas.py` | `SearchResultItem` (`mart_id` / `mart_name` / `score` / `brand?` / `price?` / `category?`) and the `SearchResponse` DTO |
| `embeddings.py` | `@lru_cache get_bedrock_embeddings(...)` returns a cached Cohere v4 query embedder (direct boto3 call, `input_type=search_query`, `output_dimension`, L2 normalization, non-blocking via `asyncio.to_thread`); `MOCK_QUERY_VECTOR` is a 1536-dim fixed unit vector |
| `client.py` | `@lru_cache get_opensearch_client()` returns an `AsyncOpenSearch` singleton; `close_opensearch_client()` is called by the lifespan shutdown |
| `repository.py` | `build_knn_body` / `build_bm25_body` pure functions build the DSL body; `hybrid_msearch` issues `msearch` and returns two sets of raw hits |
| `fusion.py` | `min_max_score_fusion` (production, weighted min-max normalized fusion) + `reciprocal_rank_fusion` (retained, unit tests only), pure functions; zero I/O, unit-testable |
| `service.py` | `SearchService.search(query, size, bm25_weight)` orchestrates weight resolution → embed → msearch → min-max fusion → DTO; the mock decision lives here (aligned with the `AgentService` pattern) |
| `router.py` | `GET /search` endpoint (with optional `bm25_weight` manual override); injects `SearchServiceDep` (`deps.py` wiring)|

**Invariant (query and doc must use the same model, same dimensions, same normalization)**: all products are embedded with Cohere Embed v4 / `output_dimension=1536` + faiss/hnsw/**innerproduct** (index `products_v5_cohere`). Cohere's float embeddings are not unit length, so both the doc side (`embed_products_os.py`) and the query side (`embeddings.py`) **L2-normalize on both ends** — `innerproduct` being equivalent to cosine requires both ends to be unit vectors; if either end isn't normalized the two vectors live in different spaces and the k-NN scores silently go all wrong. Also: the doc side uses `input_type=search_document` and the query side uses `input_type=search_query` (Cohere uses asymmetric encoding, which is key to retrieval quality for short query ↔ long product description).

**Behavior when OpenSearch is unreachable**: `get_opensearch_client()` construction issues no network connection (lazy connect). When msearch actually fires and OpenSearch is offline, `AsyncOpenSearch` raises a connection exception → propagates upward → the global handler in `main.py` converts it to HTTP 500. `search_engine` does no degradation (no degradation design, POC scope).

## 6. Data Model

### 6.1 ER Overview

```
PromptVariant (1) ──< (N) Recommendation (1) ──> (1) PipelineJob
                              │
                              └─< (N) Evaluation
```

### 6.2 PipelineJob

Tracks each pipeline run's status + ETL statistics.

| Field | Type | Purpose |
|------|------|------|
| `id` | int (PK) | |
| `customer_id` | str (indexed) | which customer |
| `brand`, `month` | str | input dimensions |
| `status` | enum | queued / cleaning / merging / analyzing / saving / evaluating / done / failed |
| `error` | str? | failure reason |
| `recommendation_id` | int? (FK) | filled in after completion |
| `rows_input/output/failed` | int? | ETL statistics |
| `cleaning_report` | JSON? | detailed ETL report |
| `raw_keys` | JSON? | which raw S3 keys it came from |
| `cleaned_dataset_key` | str? | merger output location |
| `created_at`, `updated_at` | datetime | |

### 6.3 Recommendation (JSONB hybrid pattern)

The sales recommendation produced by the LLM. **Hybrid design**: hot columns are extracted and indexed, the full payload goes into JSONB.

| Field type | Field | Purpose |
|---------|------|------|
| **Identity** | id, customer_id | |
| **Hot columns** (indexed) | customer_segment, confidence_score | high-frequency query fields |
| **Cold JSONB** | payload | full agent output (single source of truth) |
| **Schema versioning** | schema_version | tracks the payload structure version |
| **LLM metadata** | model_id, input_tokens, output_tokens, latency_ms | cost / performance tracking |
| **A/B testing** | prompt_variant_id (FK) | which prompt produced it |
| **Audit** | generated_at, pipeline_job_id | traceable |
| **HubSpot sync** | hubspot_sync_status (enum), contact_id, note_id, synced_at, sync_error, sync_retries | ⚠️ Phase 4 reserved (see note below) |

**Why JSONB hybrid**: the LLM output schema will inevitably evolve (add a field next week, bump schema_version next month); JSONB lets the DB adapt to changes with zero migration, while hot columns provide query performance.

**⚠️ Current status of the 6 HubSpot columns: Phase 4 reserved**
- The six columns `hubspot_sync_status` / `hubspot_contact_id` / `hubspot_note_id` / `hubspot_synced_at` / `hubspot_sync_error` / `hubspot_sync_retries` are reserved for the Phase 4 HubSpot Sync
- The only column with a write path right now is `hubspot_sync_status=pending` (set automatically when a recommendation is created); the other 5 columns have no runtime read/write path
- The columns are kept (forward-only migration principle); the full sync flow is enabled when HubSpot is wired in during Phase 4

### 6.4 PromptVariant (dormant — A/B testing infrastructure, currently not connected)

```
id, name, version, template, is_active, weight, notes, created_at
```

**Purpose**: a DB-managed prompt registry. The same `name` can have multiple `is_active=True` variants for A/B testing, with `weight` controlling traffic allocation.

**⚠️ Current status: dormant (table schema is ready, but there is no runtime read/write path right now)**
- The only runtime prompt source is `prompts/{module}/{version}.md` (pointed to by the `*_PROMPT_VERSION` constants in `chains/`)
- `PromptVariantRepository` exists but is not called by any service (`deps.py`'s `get_prompt_variant_repo` is kept as dormant infrastructure)
- The table schema is kept (forward-only migration principle); the read/write path will be connected when prompt A/B testing is enabled in the future

### 6.5 Evaluation (LLM-as-judge)

```
id, recommendation_id (FK), judge_model_id,
relevance_score, specificity_score, actionability_score, overall_score (indexed),
judge_reasoning, judge_input_tokens, judge_output_tokens, evaluated_at (indexed)
```

**Purpose**: use a judge LLM (typically Opus 4.7) to score the recommendation produced by the generator (Sonnet 4.5), across 4 dimensions + a free-text rationale. `overall_score` is indexed to make ranking prompt variants convenient.

**Current state**: the table schema is built; the actual judge prompt awaits real data for design.

## 7. Data Flow (full happy path)

```
HTTP POST /pipelines/run
    body: { customer_id: "C006", brand: "3c", month: "2026-05" }
    ↓
[1] api/pipelines.py
    - validate body with RunPipelineRequest
    - get PipelineService from deps
    - service.create_job() creates PipelineJob (status=queued)
    - background_tasks.add_task(service.run, job.id)
    - return JobResponse  ← HTTP 200 OK returned immediately
    ↓
═══════ HTTP response already sent ════════
    ↓
[2] PipelineService.run() in BackgroundTask
    ↓
[3] dataset_service.prepare(...) [stub]
    - should do: read S3 raw → mapper → validate → write S3 cleaned
    - currently returns mock cleaned_key + empty CleaningReport
    ↓
[4] agent_service.analyze(...)
    if mock_mode:
        return _mock_response()  ← fixed fixture
    else:
        llm = ChatBedrockConverse(...)  ← lazy init
        structured_llm = llm.with_structured_output(RecommendationOutput)
        result = await structured_llm.ainvoke(prompt)
        return result, variant_id
    ↓
[5] recommendation_repo.create_from_agent_output(...)
    - Pydantic agent_output → SQLModel Recommendation
    - payload = output.model_dump(mode="json")  ← into JSONB
    - extract hot columns (customer_segment, confidence_score)
    - INSERT INTO recommendation
    ↓
[6] job_repo.update_status('done', recommendation_id=rec.id)
    ↓
[subsequent GETs]
    GET /pipelines/{job_id}        → check status
    GET /recommendations/{rec_id}  → view LLM recommendation content
    GET /recommendations/by-customer/{customer_id} → that customer's recommendation history
```

**End-to-end latency**: mock mode ~70ms; real Bedrock ~5-15 seconds.

### 7.2 Sales Analysis Data Flow (decommissioned, see git history for implementation)

`SalesAnalysisService` and `api/analyses.py` have been removed from `src/`.
The full data flow (monthly cross-dealer ETL pipeline + Bedrock narrative output) can be found in git history.

## 8. AWS Bedrock Integration

### 8.1 Authentication

**Current approach**: after assuming the lab role, use `aws configure export-credentials --profile lab --format env` to write into `.env.local`; on FastAPI startup `set -a; source .env.local; set +a` exports the temporary credentials into the process env, and boto3 reads them automatically.

**Why not `AWS_PROFILE=lab`**: Python boto3 and the CLI have different credential refresh flows; setting PROFILE triggers an MFA prompt, which raises EOFError in a process with no stdin. **Use the temporary credential env vars directly to avoid it**.

**Key tool**: `scripts/refresh-lab-creds.sh` — automatically writes the lab profile's temporary credentials into `.env.local`; rerun it once they expire (1-12 hours).

### 8.2 Model selection

**Setting**: `BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0`

**Why the `us.` prefix**: newer Claude versions such as Sonnet 4.5 **must go through a cross-region inference profile** and cannot use the base model ID directly. The `us.` prefix lets AWS automatically route to whichever of us-east-1 / us-east-2 / us-west-2 has the most capacity at the moment.

### 8.3 Observability (Bedrock CloudWatch)

**Metrics namespace**: `AWS/Bedrock`, with the available dimension `By ModelId`.

**Key metrics**: Invocations, InvocationLatency, InputTokenCount, OutputTokenCount, InvocationClientErrors, InvocationServerErrors, EstimatedTPMQuotaUsage.

**Dual-logging trait**: the same invocation is recorded under both `us.anthropic.*` (inference profile) and `anthropic.*` (base model) ModelIds; **the token and latency metrics are only recorded under the `us.` prefix**.

**delay**: CloudWatch has a 5-15 minute propagation delay.

### 8.4 Reserved Interfaces (pending integration)

- **Guardrail**: `agent_service._guardrail_config()` already has the hook ready; once a guardrail is created in the AWS Console, just fill in `BEDROCK_GUARDRAIL_ID`
- **Invocation Logging**: off by default, enabled to ship to S3 in the production phase
- **LangSmith tracing**: set the `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY` env vars and LangChain instruments automatically

## 9. Configuration and Environment

### 9.1 Environment Variables (`.env.local`)

```bash
# Application
PORT=8000
ENVIRONMENT=dev

# Database
DATABASE_URL=postgresql+asyncpg://poc:poc@localhost:5434/marketing_cleaner

# Redis (reserved)
REDIS_URL=redis://:redispoc@localhost:6380

# AWS / LocalStack
AWS_ENDPOINT_URL_S3=http://localhost:4567   # set = use LocalStack
AWS_REGION=us-east-1

# AWS Lab credentials (written automatically by refresh-lab-creds.sh)
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
ANALYZER_MOCK_MODE=false   # true=mock fixture; false=real Bedrock

# reserved (not enabled):
# LANGSMITH_TRACING / LANGSMITH_API_KEY
# BEDROCK_GUARDRAIL_ID / BEDROCK_GUARDRAIL_VERSION
# HUBSPOT_API_KEY
```

### 9.2 Startup Sequence

```bash
# 1. Start local infra (postgres / redis / localstack / adminer)
docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres redis localstack adminer

# 2. (optional) Refresh lab credentials
./scripts/refresh-lab-creds.sh

# 3. Source temporary credentials into the shell + start FastAPI
set -a && source .env.local && set +a && unset AWS_PROFILE
uv run uvicorn recommender.main:app --reload
```

### 9.3 LocalStack S3 Structure

**Raw zone** (original files untouched + manifest):
```
s3://raw-data/
└── marketing-recommandation/                              ← S3_ROOT_PREFIX
    ├── products/                                    ← product master
    │   ├── 3c/2026/05/products.csv
    │   ├── healthy/2026/05/products.csv
    │   ├── home-appliance/2026/05/products.csv
    │   └── daily-necessities/2026/05/products.csv
    ├── customers/                                    ← customer master
    │   └── customers.csv
    └── sales/                                        ← monthly sales data (new)
        └── 2026/
            ├── 01/.keep ... 03/.keep                       ← 11 empty-month skeletons
            ├── 04/                                          ← April actual data
            │   ├── 績效追蹤4月.xlsx                          ← sales originals are not renamed
            │   ├── 手機平板資訊家電週邊(月銷售&同期).xlsx
            │   ├── 經銷-業績達成日報表(new)_zh-tw.xlsx
            │   └── _manifest.json                           ← logical→physical mapping
            └── 05/.keep ... 12/.keep
```

**Cleaned zone** (ETL + LLM output):
```
s3://cleaned-data/
└── marketing-recommandation/
    └── sales/                                         ← aligned with raw zone naming
        └── 2026/04/
            ├── region_category_summary.csv                  ← Tier 1 ETL #1
            ├── dealer_classification.csv                    ← Tier 1 ETL #2
            ├── cross_sell_gaps.csv                          ← Tier 1 ETL #3
            └── market_analysis.md                           ← Bedrock narrative
```

**Design principles**:
- **Raw immutable**: filenames kept exactly as the sales originals (Chinese, parentheses, spaces all OK); corrected versions go into a separate `04-rev2/` folder rather than overwriting
- **Cleaned uses unified English filenames**: these are consumed by programs, no need to align with the original filenames
- **Manifest pattern**: logical name (`performance-tracking`) → physical filename, so ETL doesn't need to change when filenames vary across months
- **Two-level `{YYYY}/{MM}/` partitioning**: aligns with the existing `products/{category}/{YYYY}/{MM}/` convention and is friendly to year-level lifecycle policies

**Manifest template**:
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

**`scripts/localstack/init-buckets.sh`**: on LocalStack startup it automatically creates buckets and syncs `products/`, `customers/`, and `sales/` up, excluding `~$*` (Office lock files), `.DS_Store`, and `.gitkeep`.

**Local source of truth**: the `aws-s3/` directory structure = the S3 structure (1:1 mirror); sync is just transport. Any team member can clone the repo + start LocalStack and get a consistent environment.

### 9.4 Tests (`tests/`)

```
tests/
├── conftest.py            # force mock mode (env var + patch settings), ASGI async client fixture
├── test_pipeline_e2e.py   # mock-mode full flow + 404 negative paths —— needs dev Postgres
├── test_etl_units.py      # ETL pure-function unit tests —— no DB / no network
├── test_chains.py         # chain assembly contract (fake LLM injected) —— no DB / zero Bedrock
└── test_guardrail.py      # B2 guardrail config effectiveness verification —— no DB
```

Run in two tiers:

| Tier | Command | Prerequisite |
|---|---|---|
| unit (ETL / chains / guardrail) | `uv run pytest tests/test_etl_units.py tests/test_chains.py tests/test_guardrail.py` | none |
| e2e (full pipeline) | `uv run pytest tests/test_pipeline_e2e.py` | `make infra-up` + `make migrate` |

Key conventions:
- **Never hit real Bedrock**: conftest sets `ANALYZER_MOCK_MODE=true` before importing the app, and after import directly patches `settings.analyzer_mock_mode = True` (double protection — `main.py`'s `load_dotenv(override=True)` would otherwise override the env var with `.env.local`).
- **Chain tests cannot use `FakeListChatModel`**: both chains go through `with_structured_output` (`bind_tools` under the hood), and neither of langchain-core's fake models implements `bind_tools`; instead use a custom `FakeStructuredChatModel(GenericFakeChatModel)` that stubs `bind_tools` to feed an `AIMessage` with `tool_calls`.
- **e2e uses real Postgres, not SQLite**: JSON column and datetime semantics silently lose accuracy under SQLite.

## 10. Phase Plan

| Phase | Scope | Status |
|-------|------|------|
| **0** | Scaffolding (three-layer architecture + 4 services + 4 tables + Mock analyzer)| ✅ done |
| **1** | Real Bedrock integration (authentication + Sonnet 4.5 + structured output)| ✅ done |
| **1.5** | Real ETL logic | ✅ **done (scope pivot)** |
| **1.6** | ~~Sales analysis module (`/analyses/sales`) + Bedrock narrative~~ (decommissioned) | ✅ **done then removed** |
| **Architecture convergence** | layer boundary fixes + dead-code cleanup + chains/ extraction + doc sync | ✅ done |
| **2 (search)** | Hybrid search API (`src/search_engine/`) — Cohere v4 embedding + BM25+k-NN+min-max fusion + `GET /search` endpoint | ✅ done (2026-06-13) |
| **2** | Enable prompt management (populate the PromptVariant table + write the first prompt)| ⏸ waiting for Phase 3 |
| **3** | Evaluation pipeline (LLM-as-judge / A/B statistics)| ⏸ after business validation |
| **4** | SharePoint → S3 sync script (replacing manual seeding) | ⏸ after business validation |
| **5** | HubSpot Renderer + Sync (transform → Properties + Note)| ⏸ after business validation |
| **6** | Production hardening (structlog / RequestID / retry / pre-commit / persistent analyses table)| ⏸ only after the POC ends |

**Phase 1.5 scope pivot note**: the original plan (see [data-governance.md](../plans/data-governance.md)) was to turn `DatasetService.prepare()` from a stub into real ETL feeding personalized recommendations. During the actual session it turned out what the business really needs is "**monthly cross-dealer market analysis**" (not per-customer recommendation), so `SalesAnalysisService` was built running a completely different pipeline. That module was later removed during architecture convergence (only git history remains). `DatasetService.prepare()` is still a stub (the per-customer flow has not been started). See [data-governance.md §9 actual outcome](../plans/data-governance.md#9-outcome).

## 11. Design Principles Summary

1. **Boundary validation**: enforce Pydantic validation at the HTTP edge, trust types internally
2. **Single source of truth**: the DB stores JSONB (the full LLM output), not pre-rendered for HubSpot
3. **Lazy on abstraction**: write concrete classes directly, add a Protocol only when there is a real multi-implementation need
4. **Schema-as-data**: the same Pydantic schema serves as the LLM contract / API DTO / DB write validation
5. **Mock mode for parallel development**: use `ANALYZER_MOCK_MODE` so ETL/DB logic isn't blocked by Bedrock permissions
6. **POC scope discipline**: no worker / no ABC / no interface / no abstract class — if genuinely needed, it's a 5-minute add

## 12. Key Reference Documents

- `README.md` — setup steps and startup commands
- `docker-compose.dev.yml` — full local infra definition (aligned with intellio.ai conventions)
- `pyproject.toml` — package list and version constraints
- `alembic/versions/*.py` — DB schema migration history
- `scripts/refresh-lab-creds.sh` — AWS lab credential refresh tool
- `scripts/localstack/init-buckets.sh` — LocalStack S3 startup initialization (incl. sales/ sync)
- `src/recommender/chains/` — LCEL chain factory (`build_recommendation_chain` / `build_judge_chain`)
- `src/recommender/services/promo_forecast_service.py` — monthly key-account promotion forecast service (451 lines, orphan service, see §5.7)
- ~~`src/recommender/services/sales_analysis_service.py`~~ — decommissioned, see git history
- ~~`src/recommender/api/analyses.py`~~ — decommissioned, see git history
