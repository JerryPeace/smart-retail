# Spec: product-search-hybrid-api

This spec defines the six contracts that MUST hold once Phase 2 (the hybrid search API) is complete: the search contract, the layering contract, the embedding contract, the async contract, the testing contract, and the safety contract. Once implementation is done, any artifact that violates a Requirement below is considered to have failed acceptance. Aligned with `docs/plans/product-search-vectorization.md` §Phase 2 and the finalized decisions in this change's design.md (application-side RRF, domain module, mock by default).

## ADDED Requirements

### Requirement: search contract — `GET /search` hybrid fusion, empty results 200, size upper bound

`GET /search` SHALL accept `q` (required, `min_length=1`) and `size` (default 10, range 1–100), and against `products_v1` **simultaneously** execute two queries — k-NN (query vector) and BM25 (`multi_match` targeting `martName`/`feature`/`keyword`) — with each route fetching 2×size candidates, then fuse them with **application-side Python RRF** (`score(doc)=Σ 1/(k+rank)`, k=60) and return the top-size. The response SHALL be a `SearchResponse` (`query` + `results: list[SearchResultItem]`), with `results` sorted by RRF fusion score descending; `SearchResultItem.score` SHALL be the RRF fusion score rather than the OpenSearch `_score` (the two routes' `_score` values are not dimensionally comparable). No results SHALL return **HTTP 200 + empty `results`**, and SHALL NOT return 404 — "search found nothing" is a normal business outcome. Fusion SHALL NOT use an OpenSearch search pipeline / score-ranker-processor (application-side RRF is finalized).

#### Scenario: hybrid fusion takes effect
- **WHEN** querying with `GET /search?q=robot vacuum` (OpenSearch already loaded with the Phase 1 dataset of 26,014 records + vectors)
- **THEN** return 200, with `results` non-empty, sorted by `score` descending, and the result set being the top-size of the union of the k-NN and BM25 candidate routes after RRF fusion (documents hit by both routes have their scores summed and rank higher)

#### Scenario: empty result is not 404
- **WHEN** querying a string that neither route hits (e.g. random garbage)
- **THEN** return HTTP 200 with `results == []`, without raising an exception and without returning 404

#### Scenario: parameter boundaries
- **WHEN** the request has `size=101` or `size=0` or `q=` (empty string)
- **THEN** FastAPI validation returns 422; `size=1` returns exactly 1 entry

#### Scenario: RRF pure-function correctness
- **WHEN** evaluating `reciprocal_rank_fusion([["a","b"],["b","c"]], k=60)`
- **THEN** `b`'s score is `1/61 + 1/62` and it ranks first; tied documents break by doc_id lexicographic order, and repeated execution on the same input yields exactly identical results (deterministic)

### Requirement: layering contract — three-layer discipline within the domain module, wiring centralized in deps.py

`src/recommender/search/` SHALL be a self-contained domain module (router / service / repository / schemas / rrf / embeddings / client), and within the module SHALL follow the existing three-layer discipline: the router SHALL only do parameter validation and call the service, SHALL NOT import `SearchRepository` / `AsyncOpenSearch`, and SHALL NOT raise `HTTPException` (unexpected errors are delegated to the global handler in `main.py`); the service SHALL return a Pydantic DTO (`SearchResponse`) and SHALL NOT leak raw OpenSearch hit dicts to the router; the repository SHALL only do DSL construction and msearch I/O, SHALL NOT read `settings` (`os_client` and `index` are injected by deps), and SHALL NOT contain fusion or DTO-conversion logic. DSL body construction (`build_knn_body` / `build_bm25_body`) and RRF SHALL be module-level pure functions, importable and testable without a client. All DI providers (`get_os_client` / `get_search_repository` / `get_search_service`) SHALL be defined in `deps.py` (the single wiring point), and SHALL NOT establish additional wiring inside the search module.

#### Scenario: router does not cross layers
- **WHEN** checking `grep -rn "SearchRepository\|AsyncOpenSearch\|HTTPException" src/recommender/search/router.py`
- **THEN** 0 matches

#### Scenario: repository does not read global settings
- **WHEN** checking `grep -n "from recommender.config import settings" src/recommender/search/repository.py`
- **THEN** 0 matches

#### Scenario: single wiring point
- **WHEN** checking `grep -rn "def get_search\|def get_os_client" src/recommender/`
- **THEN** only `deps.py` matches

### Requirement: embedding contract — query/doc same model, same parameters, same dimensions, mock path

The query embedding SHALL use exactly the same model and parameters as the Phase 1 doc side: `amazon.titan-embed-text-v2:0`, `dimensions=1024`, `normalize=true` — this is the vector-space consistency invariant (the precondition for innerproduct to be equivalent to cosine); `normalize=true` SHALL be hardcoded in the embeddings builder and SHALL NOT be made an adjustable Setting. The embeddings client SHALL be constructed via an `@lru_cache` builder (mirroring the `llm.py` pattern, shared at the process level), with region from `bedrock_embed_region` (ap-northeast-1, configured separately from the LLM's us-east-1). When `analyzer_mock_mode=true`, the service SHALL return a fixed 1024-dimensional **unit vector** (`MOCK_QUERY_VECTOR`) and SHALL NOT issue any Bedrock call; under mock, the BM25 route executes as usual. Switching the embedding model SHALL be accompanied by a full re-embed + new index (reindex/alias) and SHALL NOT be done by just changing Settings.

#### Scenario: mock mode zero Bedrock
- **WHEN** calling `GET /search?q=any query` under `ANALYZER_MOCK_MODE=true`
- **THEN** end-to-end returns 200 (k-NN executes legally with `MOCK_QUERY_VECTOR`, BM25 executes for real, RRF fuses normally), with zero Bedrock calls throughout and no AWS credentials required

#### Scenario: mock vector invariant
- **WHEN** inspecting `MOCK_QUERY_VECTOR`
- **THEN** length == 1024 and L2 norm == 1.0 (keeping the innerproduct semantics legal)

#### Scenario: real embedding parameters consistent
- **WHEN** issuing a query embedding request with mock OFF
- **THEN** the model is `amazon.titan-embed-text-v2:0`, the request contains `dimensions=1024` and `normalize=true`, and the returned vector has length 1024 — exactly the same parameters as the doc-side embedding in `scripts/etl/embed_products_os.py`

### Requirement: async contract — do not block the event loop, client lifecycle managed by lifespan

The entire search chain SHALL be async: OpenSearch access SHALL use `AsyncOpenSearch` (`opensearch-py[async]`, aiohttp connection) and SHALL NOT use the synchronous `OpenSearch` client on the async path; the two queries SHALL be issued as a single `msearch` (one round-trip, server-side parallelism), and any per-response error from msearch SHALL fail fast and propagate (the global handler converts it to 500), SHALL NOT silently degrade to a single route. Real embedding calls SHALL go through `aembed_query` (executor-wrapped) and SHALL NOT call the synchronous `embed_query` directly on the event loop. The AsyncOpenSearch client SHALL be a process singleton (`@lru_cache` builder), constructed at lifespan startup (lazy connect, OpenSearch being offline does not block app startup), and at shutdown SHALL `await client.close()` to release the aiohttp session.

#### Scenario: app startup does not depend on OpenSearch being online
- **WHEN** starting uvicorn in mock mode while the OpenSearch container is not running
- **THEN** the app starts normally (the client is constructed without connecting); hitting `/search` at this point returns 500 (connection failure goes through the global handler), and the app does not crash

#### Scenario: clean shutdown
- **WHEN** uvicorn receives a shutdown signal
- **THEN** lifespan shutdown runs `await close_opensearch_client()`, with no aiohttp `Unclosed client session` warning in the log

#### Scenario: single msearch
- **WHEN** handling one `/search` request
- **THEN** only a single `_msearch` request is issued to OpenSearch (containing both the k-NN and BM25 sub-queries), rather than two independent `_search` requests

### Requirement: testing contract — unit pure functions + mock smoke + opt-in accuracy

Tests SHALL be in three layers: (1) `tests/test_search_units.py` SHALL cover RRF (fusion correctness, the k parameter, empty lists, one-sided gaps, tie-break determinism), DSL builder structure, the mock vector invariant, and `SearchService` orchestration (fake repo injection), and SHALL NOT require docker / network / AWS credentials; (2) `tests/test_search_api_smoke.py` SHALL, in mock mode, verify 200 + structure + fusion evidence + parameter boundaries + empty-result 200 for `GET /search`, requiring OpenSearch (with Phase 1 data in place), and when OpenSearch is unreachable SHALL skip rather than fail (aligned with the `test_pipeline_e2e.py` convention), and SHALL NOT require Bedrock; (3) accuracy evaluation SHALL be an opt-in script (`scripts/etl/judge_hybrid_search.py`): reusing the approved golden set (status gate enforced programmatically) and the LLM-judge scale, placing hybrid / k-NN-only / BM25-only side by side in three columns within the same run, with the success criterion being that hybrid is no worse than any single method and complementarity is preserved (vector-strong and BM25-strong queries are both non-zero); if not met it SHALL report faithfully and SHALL NOT loosen the judgment. The existing tests (`test_etl_units.py` / `test_chains.py` / `test_pipeline_e2e.py` / `test_product_search_units.py`) SHALL remain all green.

#### Scenario: unit tests independent of infrastructure
- **WHEN** running `uv run pytest tests/test_search_units.py` in an environment where the OpenSearch / Postgres containers are all stopped and there are no AWS credentials
- **THEN** all pass, with zero network calls

#### Scenario: smoke skips when infrastructure is missing
- **WHEN** running `uv run pytest tests/test_search_api_smoke.py` while OpenSearch is unreachable
- **THEN** the whole module is skipped (with a prerequisite-command hint), not failed

#### Scenario: accuracy evaluation gate
- **WHEN** running `judge_hybrid_search.py` against a golden set with `meta.status: draft`
- **THEN** it exits 1 immediately, without issuing any Bedrock or OpenSearch request

#### Scenario: hybrid no worse than any single method
- **WHEN** completing the three-column evaluation against the approved golden set (15 entries)
- **THEN** the report Summary shows the hybrid global relevant count ≥ max(vec-only, bm25-only), and the hybrid relevant counts for both q04 (ThinkPad, BM25-strong) and the contextual query (vector-strong) are > 0

### Requirement: safety contract — mock by default, real embedding cost disclosure, zero DB impact

`analyzer_mock_mode` SHALL remain `true` by default, and hitting `/search` in local development SHALL NOT incur Bedrock charges. Any mock-OFF real embedding / LLM-judge run (including the Phase 7 accuracy evaluation) SHALL disclose the estimated cost to the user in advance and obtain consent (safety.md §1); expired lab credentials SHALL be refreshed with `scripts/refresh-lab-creds.sh`, SHALL NOT be hand-edited in `.env.local`, and no log / report / commit SHALL print an AWS access key. This change SHALL NOT touch the PostgreSQL schema: zero alembic migrations (`alembic current` identical before and after, no new files in `alembic/versions/`). Dependency changes SHALL be limited to adding the `[async]` extra to `opensearch-py`.

#### Scenario: cost gate
- **WHEN** an agent is about to run the Phase 7 accuracy evaluation (real Bedrock)
- **THEN** a "cost-estimate disclosure + explicit user consent" record exists in the conversation; otherwise it must not run

#### Scenario: zero DB impact
- **WHEN** comparing `alembic current` and `alembic/versions/` before and after implementation
- **THEN** the revision is identical and there is no new migration file

#### Scenario: zero cost by default
- **WHEN** a developer starts the app in the default environment (`ANALYZER_MOCK_MODE=true`) and repeatedly calls `/search`
- **THEN** the AWS bill has zero increment (no Bedrock calls whatsoever)
