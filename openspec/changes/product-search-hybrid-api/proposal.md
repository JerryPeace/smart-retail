# product-search-hybrid-api

> Upstream plan: `docs/plans/product-search-vectorization.md` §Phase 2 (hybrid + search API, now empirically backed by Phase 1).
> Predecessor change: `openspec/changes/product-search-vectorization/` (Phase 1, ✅ closed 2026-06-13) — 26,014 products loaded into the local docker OpenSearch `products_v1` and 100% Titan v2 vectorized (1024 dims, normalize:true).
> This change upgrades the Phase 1 POC scripts into a **hybrid search API built into the app**.

## Why

The Phase 1 closing data (three-round scoring scale, accepted by the Opus 4.8 judge) gives a clear conclusion: **no single search method is enough; hybrid is the right answer**:

1. **Vector strength, proven — situational/symptom-style queries**: "cold hands and feet outdoors in winter" vec 4:0, "hair falling out too much, want it fuller" vec 7:2. For body-state descriptions with zero lexical overlap, BM25 is wiped out while vectors work.
2. **BM25 strength, proven — brand/model-style queries**: "ThinkPad laptop" vec 1:10 — embeddings get diluted by spec/category text, while BM25 hits lexically with precision. Both judges fully agreed here (each 0:10 / 1:10), which is a structural blind spot of vectors.
3. **Complementarity, quantified — the direct evidence base for hybrid**: globally, vec_only_rel vs bm25_only_rel, Haiku judge **57 vs 73**, Opus judge **41 vs 52**, both judges pointing the same direction — each method finds **dozens** of relevant products the other misses. Shipping only one method means throwing away the entire batch of relevant results from the other side.

But the Phase 1 deliverables are one-off verification scripts under `scripts/etl/` (synchronous boto3, printed reports), not a service that downstream can consume. To make product search an app capability (for future recommendation scenarios, HubSpot, and other downstream consumers), we need:

- A **`GET /search` endpoint** that follows the three-layer discipline, runs dual BM25 + k-NN queries, fuses with RRF, and returns a structured DTO;
- Query embedding integrated into the app's Bedrock configuration (with a **mock path** — otherwise every local-dev hit to /search burns real Bedrock, violating safety.md);
- Verification that **hybrid is no worse than any single method**, using the golden set + LLM-judge scale left over from Phase 1.

## What Changes

Aligned with the §Phase 2 outline in the plan document and the three decisions locked in this round (scope = core hybrid search API only; fusion = application-side Python RRF; query embedding = app Bedrock config + mock path):

- **Domain module `src/recommender/search/`** (a self-contained bounded context, a deliberate exception locked in plan §Phase 2, see design §1):
  - `schemas.py` — `SearchResultItem` / `SearchResponse` DTOs and query-parameter constraints (`q` required, `size` defaults to 10 with a cap of 100).
  - `repository.py` — `SearchRepository(os_client, index)`: **AsyncOpenSearch** + `msearch` running k-NN and BM25 as two concurrent queries in one shot; the DSL body construction is extracted into pure functions (`build_knn_body` / `build_bm25_body`, lifted from `scripts/etl/verify_search_os.py` and converted to async).
  - `rrf.py` — `reciprocal_rank_fusion(...)` pure function (`score(doc) = Σ 1/(k+rank)`, k defaults to 60), easy to unit test, zero I/O.
  - `service.py` — `SearchService(repo)`: embed query (mock mode returns a fixed vector) → hybrid msearch → RRF fusion → top-size → map to DTO. Returns an empty list when there are no results (**200, not 404**).
  - `embeddings.py` — `@lru_cache get_bedrock_embeddings(...)` returning a langchain-aws `BedrockEmbeddings` (following the cached-builder pattern of `llm.py`).
  - `router.py` — `GET /search`, injecting `SearchServiceDep`.
- **New settings** (`config.py`): `opensearch_host` / `opensearch_index` / `bedrock_embed_model_id` / `bedrock_embed_region` (Titan is in the Tokyo lab, different from the LLM's `bedrock_region=us-east-1`) / `embed_dimensions`. Mock reuses the existing `analyzer_mock_mode`; no new flag is added.
- **App integration**: `main.py` lifespan startup builds the AsyncOpenSearch client + preheats the embeddings client when not in mock mode, and shutdown does an **async close** of the client; `deps.py` (the single wiring point) gains `get_opensearch_client` / `get_search_repository` / `get_search_service` providers; `main.py` calls `include_router(search.router)`.
- **Dependency change (the only one allowed this round)**: `opensearch-py` is re-declared as `opensearch-py[async]` (the async client needs aiohttp, the installation method specified by the official async guide; aiohttp is already a transitive dependency of aioboto3, so this just makes the declaration explicit).
- **Three-tier testing** (aligned with existing conventions):
  1. **Unit (CI, no docker, no network)** `tests/test_search_units.py`: RRF pure function, DSL builders, mock-vector invariants.
  2. **mock-mode API smoke (needs OpenSearch, not Bedrock)**: `GET /search` returns 200 + correct structure + fuses both sides; the whole module is skipped when OpenSearch is unreachable (aligned with the `test_pipeline_e2e.py` pattern).
  3. **Accuracy evaluation (opt-in, real Bedrock + OpenSearch, money-gated)**: reuse the golden set (15 approved queries) + the LLM-judge method (`judge_search_relevance.py` scale, Opus-class judge) to evaluate the relevance of the `/search` hybrid endpoint, running hybrid / k-NN / BM25 side by side in three columns in the same round, verifying that hybrid is no worse than any single method.
- **Zero alembic migration**: search runs entirely on OpenSearch and never touches the PostgreSQL schema (a mandatory verification item).

## Out of Scope (explicitly not done this round)

| Item | Why not |
|------|-----------|
| `category` / `stock` soft-signal demotion | **Phase 2b**. Ship the core hybrid first to establish a baseline, then layer on ranking signals (later part of plan §Phase 2) |
| Chinese embedding model benchmark (Titan vs Cohere Multilingual) | **Phase 2b**. The golden set is kept as a fixed scale; the benchmark does not block API construction |
| OpenSearch native search pipeline (score-ranker-processor / normalization-processor) | Locked on **application-side Python RRF**: it can reuse the POC's knn/bm25 queries, and RRF is a pure function that is easy to unit test (trade-off in design §9) |
| Fixing classification pollution (FM re-classifying 363 brand-store products) | **Phase 3** (plan §Phase 3) |
| HubSpot sync, wiring recommendation scenarios to /search | Downstream consumption is a follow-up change; this round only delivers a consumable API |
| Going to prod, aligning with prod OpenSearch | The POC is not tied to prod (positioning unchanged) |
| POST /search, pagination cursor, filter parameters | Simplicity First: the core contract is `GET /search?q=&size=`; the rest waits for real demand |
| Modifying the three Phase 1 scripts under `scripts/etl/` | The query functions are reused by "lifting them into the search module and converting to async"; the original scripts are kept as ETL/rebuild tools and are not refactored |
| Adding an alembic migration / touching PostgreSQL | This round's data layer only reads OpenSearch (a verification item) |
