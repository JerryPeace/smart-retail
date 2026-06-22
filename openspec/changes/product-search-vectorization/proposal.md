# product-search-vectorization

> Upstream plan: `docs/plans/product-search-vectorization.md` (approved; decisions D1–D8 are settled). This OpenSpec change is the **specification and task breakdown of Phase 1 (P1-1 ~ P1-5) of that plan**; it does not redesign anything. If anything here conflicts with the plan, the plan document takes precedence.

## Why

The company's catalog of 26,018 products suffers from **category contamination**: ≥363 products from brand flagship stores (Grape King, FPG Biotech, Sun Ten, sakuyo, MEGA KING, Takashimaya) stuff the brand name into `categoryLevel1`, while `categoryLevel2` is polluted with marketing labels like "ingredient category / hot-sale campaign". The consequences are:

1. **The category filter misses products** — searching "health supplements" with a category filter misses Grape King Lingzhi King (its category is the brand name "Grape King", not "health"). Any downstream recommendation/search that relies on categories inherits this hole.
2. **Chinese BM25 cannot support semantic queries** — the source data is 100% Traditional Chinese, and for queries with no surface-form overlap like "a drink that boosts immunity", keyword matching cannot find lingzhi beverages.

**POC business thesis**: semantic search (vectors) extracts meaning from `martName`/`feature` without depending on the dirty categories, so it can find products that BM25 and the category filter cannot. This change aims to **quantitatively validate** this on **local docker OpenSearch + Bedrock Titan v2** (side-by-side comparison on a golden set), rather than just running a demo.

**Positioning**: a POC, not bound to going to prod. The implementation follows AWS best practices as much as possible (OpenSearch 2.19.x, faiss, innerproduct), with versions not constrained by prod.

## What Changes

Aligned with the plan's work-item numbering P1-1 ~ P1-5, plus the two process decisions made this round:

- **P1-1 local OpenSearch (docker)** — add an `opensearch` service to `docker-compose.dev.yml` (2.19.x, single-node, security off, JVM 1g, healthcheck, smartcn plugin) on port 9200; Dashboards on 5601 is an optional profile. Aligned with the healthcheck conventions of the existing services.
- **P1-2 build the k-NN index `products_v1`** — `index.knn=true`, text fields use the smartcn analyzer, `embedding` is a `knn_vector` (1024 dims, faiss, hnsw, innerproduct, aligned with D2/D3).
- **P1-3 load raw data** — add `scripts/etl/load_products_os.py`: first probe the JSON structure → filter `isSearchable=1` → bulk index (`_id=martId`, D5, naturally idempotent). Pure algorithm, zero LLM (ETL First).
- **P1-4 Titan v2 vectorization** — add `scripts/etl/embed_products_os.py`: boto3 lab profile calls Bedrock directly (D7/D8), text cleaning → batched embedding → bulk update written back; retry/backoff, resume mechanism (only fills in docs without an embedding), 5–10 concurrent. **Hits real Bedrock (~$0.1 one-time); the user must be notified before running (safety gate)**.
- **golden set: drafted by the agent, reviewed by the user** (settled) — during implementation, the agent drafts 15–20 queries from the 26k product data (two classes: surface-form overlap + no surface-form overlap) plus an expected-hit list, stored as structured YAML (query/category/expected_mart_ids/rationale). **P1-5 verification must not run until the user has approved the review** (tasks set an explicit gate).
- **P1-5 verify search results** — add `scripts/etl/verify_search_os.py`: embed each query with the same Titan v2 → compare k-NN query vs BM25 `match` side by side on the top-10, plus a category-contamination demo with the category filter. Success criterion: among the no-surface-overlap queries, "found by vectors, not found by BM25" ≥ 3 cases. Outputs a retainable comparison report; the golden set is reused for the Phase 2 benchmark.
- **pure-function tests** (settled) — test only deterministic pure functions: text cleaning (strip HTML, `keyword` null handling, truncate), JSON structure parsing, embedding-text assembly. Aligned with the `tests/test_etl_units.py` convention; no network, no docker. **OpenSearch / Bedrock I/O is not tested.**
- **prerequisite miscellany** — add a source-file rule to `.gitignore` (36MB stays out of the repo); `uv add opensearch-py`; the source file `products/OpenSearch_Full_20260612_030007.json` is placed in by the **user** (does not currently exist; it is a runtime blocker).

## Out of Scope (explicitly not done this round)

Everything in §6 of the plan, plus the additions in this OpenSpec:

| Item | Why not |
|------|-----------|
| Going to prod; replicating prod's RDS→event→OpenSearch sync | The POC is not bound to prod; the JSON is loaded directly into local OpenSearch (plan §6) |
| pgvector | Since we want to practice the OpenSearch ecosystem, use OpenSearch directly (plan §6) |
| Bedrock Knowledge Base | A RAG document-QA abstraction, not product-ranking search; wrong abstraction (plan §6) |
| OpenSearch Bedrock connector (approach B) | Heavy local setup; the POC uses approach A, self-embedding via boto3 (D7, plan §6) |
| Hybrid fusion (RRF) / API endpoint / three-layer wiring | **Phase 2**. This round only has POC scripts; do not build `repositories/product_search_repo.py`, `services/search_service.py`, or `api/search.py` (the three layers appear only in plan §5 Phase 2) |
| Fixing category contamination (FM re-classifying the 363 brand-store products) | **Phase 3** (plan §5) |
| Pre-stocking backup vectors; multi-model comparison (Titan vs Cohere) | Phase 1 uses a single Titan v2; the Chinese-model benchmark is left to Phase 2 to test recall@10 with the golden set (D3, plan §6) |
| Wrapping embedding in LangChain | Embedding is an atomic operation, called directly via boto3 (D8); LangChain is left for Phase 3 |
| Automated tests of OpenSearch / Bedrock I/O | Settled to test only pure functions; I/O verification uses the manual criteria in tasks (curl/_count) |
| Writing product data into PostgreSQL / adding an alembic migration | This round, data only goes into OpenSearch; zero DB schema changes |
| Bedrock Batch Inference | Half-price but a heavy process; for the POC's volume, on-demand is reasonable (this trade-off is already recorded in plan P1-4) |
