# product-search-hybrid-api — Tasks

> Ordering principle: bottom-up (config/dependencies → pure-function core → I/O → service → router → app integration → tests → evaluation → verification). After each phase completes, the app should still be startable (`uvicorn recommender.main:app`).
>
> **Infrastructure requirement markers**:
> - 🟢 No docker / no network (runnable in CI)
> - 🟠 Requires OpenSearch (`docker compose -f docker-compose.dev.yml --env-file .env.local up -d opensearch`; the Phase 1 volume already contains 26,014 records + vectors)
> - 🔴 Requires real Bedrock (**costs money; you MUST inform the user and obtain consent before running** — safety.md §1)
>
> ⚠️ This change **should not produce any new alembic migration** (search uses OpenSearch, not Postgres). If any task gets halfway and you find that "a migration seems to be needed," stop and confirm with the user — that signals a deviation from the planned scope.

## Phase 1 — config and dependencies 🟢

- [x] **1.1** `pyproject.toml`: change `"opensearch-py>=3.2.0"` to `"opensearch-py[async]>=3.2.0"`, and update with `uv lock` (the only dependency change permitted in this work; aiohttp should already be a transitive dependency of aioboto3, declared explicitly here).
      ✅ Criterion: `uv run python -c "from opensearchpy import AsyncOpenSearch; print('ok')"` outputs `ok`.
- [x] **1.2** `src/recommender/config.py`: add an OpenSearch section to `Settings` (`opensearch_host="http://localhost:9200"`, `opensearch_index="products_v1"`) and a Bedrock Embedding section (`bedrock_embed_model_id="amazon.titan-embed-text-v2:0"`, `bedrock_embed_region="ap-northeast-1"`, `embed_dimensions=1024`), aligned with design §2.
      ✅ Criterion: `uv run python -c "from recommender.config import settings; print(settings.opensearch_index, settings.bedrock_embed_region)"` outputs `products_v1 ap-northeast-1`; after setting `OPENSEARCH_INDEX=test_v2`, rerunning outputs `test_v2` (env binding takes effect and is not swallowed by `extra="ignore"`).

## Phase 2 — search module pure-function core 🟢

- [x] **2.1** Create `src/recommender/search/__init__.py` and `search/schemas.py`: `SearchResultItem` (`mart_id: str`, `mart_name: str`, `score: float`, `brand: str | None`, `price: float | None`, `category: str | None`), `SearchResponse` (`query: str`, `results: list[SearchResultItem]`).
      ✅ Criterion: `uv run python -c "from recommender.search.schemas import SearchResponse; print(SearchResponse(query='x', results=[]).model_dump())"` succeeds.
- [x] **2.2** `search/rrf.py`: `reciprocal_rank_fusion(result_lists: Sequence[Sequence[str]], k: int = 60) -> list[tuple[str, float]]`, formula `score(doc)=Σ 1/(k+rank)` (rank starts from 1), sorted by score descending with ties broken by doc_id lexicographic order; empty lists / one-sided gaps are legal. Pure function, zero OpenSearch imports (design §7).
      ✅ Criterion: `grep -n "opensearch\|pydantic" src/recommender/search/rrf.py` returns 0 matches; manual REPL verification that in `reciprocal_rank_fusion([["a","b"],["b","c"]])`, `b`'s score == 1/61 + 1/62 and ranks first.
- [x] **2.3** `search/embeddings.py`: `@lru_cache get_bedrock_embeddings(model_id, region, profile, dimensions)` returns a langchain-aws `BedrockEmbeddings` (`model_kwargs={"dimensions": ..., "normalize": True}`, normalize hardcoded — design §3/§4); `MOCK_QUERY_VECTOR = [1.0] + [0.0]*1023` constant.
      ✅ Criterion: `grep -n "lru_cache" src/recommender/search/embeddings.py` matches; `uv run python -c "from recommender.search.embeddings import MOCK_QUERY_VECTOR; assert len(MOCK_QUERY_VECTOR)==1024 and sum(v*v for v in MOCK_QUERY_VECTOR)==1.0"` passes (no real client built, zero network).
- [x] **2.4** `search/repository.py` pure-function part: `build_knn_body(vector, k)` / `build_bm25_body(query_text, k)`, with DSL aligned to the `knn_search` / `bm25_search` in `scripts/etl/verify_search_os.py` (k-NN targets the `embedding` field; BM25 `multi_match` targets `martName`/`feature`/`keyword`).
      ✅ Criterion: both functions are module-level pure functions (not methods), importable without building a client to assert the dict structure directly.

## Phase 3 — repository I/O / service / router 🟢 (writing does not require OpenSearch; behavior verification is in Phase 6)

- [x] **3.1** `search/client.py`: `@lru_cache(maxsize=1) get_opensearch_client() -> AsyncOpenSearch` (`hosts=[settings.opensearch_host]`, local security off, no auth) + `async def close_opensearch_client()` (only `await client.close()` if the cache holds a value, then `cache_clear()`) — design §9.1.
      ✅ Criterion: `uv run python -c "from recommender.search.client import get_opensearch_client; c=get_opensearch_client(); assert get_opensearch_client() is c"` (singleton; lazy-connect construction does not require OpenSearch to be online).
- [x] **3.2** `search/repository.py` I/O part: `SearchRepository(os_client, index)`, `async def hybrid_msearch(vector, query_text, k) -> tuple[list[dict], list[dict]]` — the msearch body is the interleaved list `[{"index": idx}, knn_body, {"index": idx}, bm25_body]`, returning the hits from `responses[0]`/`responses[1]`; if either response contains an `error` → raise (fail fast, design §6.2). The repository does not read `settings`.
      ✅ Criterion: `grep -n "from recommender.config import settings" src/recommender/search/repository.py` returns 0 matches; `grep -n "msearch" src/recommender/search/repository.py` matches.
- [x] **3.3** `search/service.py`: `SearchService(repo)` — `__init__` reads `settings.analyzer_mock_mode` (aligned with the AgentService pattern); `async def search(query, size=10) -> SearchResponse`: `_embed_query` (mock → `MOCK_QUERY_VECTOR`; real → `get_bedrock_embeddings(...).aembed_query(query)`) → `hybrid_msearch(vector, query, k=2*size)` → `reciprocal_rank_fusion` → top-size → `_id→_source` map join into `SearchResultItem` (score=RRF score) → `SearchResponse`. No results returns an empty list without raising.
      ✅ Criterion: `grep -n "aembed_query" src/recommender/search/service.py` matches (does not use synchronous `embed_query`, which would block the event loop); `grep -n "HTTPException\|raise NotFoundError" src/recommender/search/service.py` returns 0 matches; the return type annotation is `SearchResponse`, not dict/raw hits.
- [x] **3.4** `search/router.py`: `APIRouter(prefix="/search", tags=["search"])`, `GET ""`: `q: str = Query(min_length=1)`, `size: int = Query(default=10, ge=1, le=100)`, inject `SearchServiceDep`, `response_model=SearchResponse`. No try/except, no raising HTTPException.
      ✅ Criterion: `grep -n "HTTPException\|SearchRepository\|AsyncOpenSearch" src/recommender/search/router.py` returns 0 matches (the router does not touch the repo/client).

## Phase 4 — app integration (deps / lifespan / router mounting) 🟢

- [x] **4.1** `src/recommender/deps.py`: add `get_os_client` (returns the `get_opensearch_client()` singleton) + `OSClientDep`, `get_search_repository(os_client)` (passing in `settings.opensearch_index`) + `SearchRepoDep`, `get_search_service(repo)` + `SearchServiceDep` — deps.py remains the single wiring point (design §9.3).
      ✅ Criterion: `grep -rn "Depends(" src/recommender/search/` matches only the `SearchServiceDep` usage in router.py (all wiring definitions live in deps.py); app import has no DI errors.
- [x] **4.2** `src/recommender/main.py`: (a) add `get_opensearch_client()` (builds the object, lazy connect) and `_preheat_embeddings()` (skipped in mock; on failure only logs a warning, mirroring `_preheat_llm`) to lifespan startup; (b) add `await close_opensearch_client()` to shutdown (update the "Shutdown does nothing" comment); (c) `app.include_router(search.router)`.
      ✅ Criterion: `ANALYZER_MOCK_MODE=true uv run uvicorn recommender.main:app` still starts and shuts down normally when **OpenSearch is not running** (lazy client construction, mock preheat skipped, shutdown close raises no unhandled exception); `GET /search` appears in OpenAPI `/docs`.
- [x] **4.3** Regression check: existing tests are not broken by the integration.
      ✅ Criterion: `uv run pytest tests/test_etl_units.py tests/test_chains.py tests/test_product_search_units.py` all green (🟢 no docker required).

## Phase 5 — unit tests 🟢

- [x] **5.1** `tests/test_search_units.py` — RRF: manual verification of two-list fusion (`b` in both lists → 1/61+1/62 ranks first), the effect of varying the `k` parameter on the ranking scores, empty input (`[]` and `[[], []]`) returns empty, one-sided gap (one list empty) equals single-route ranking, deterministic tie-break (same input run twice yields identical results).
      ✅ Criterion: `uv run pytest tests/test_search_units.py -k rrf` all green, zero network and zero docker.
- [x] **5.2** Same file — DSL builder: `build_knn_body` contains `query.knn.embedding.vector` (passed through verbatim) and `size==k`; `build_bm25_body` contains `multi_match.fields == ["martName","feature","keyword"]`.
      ✅ Criterion: `uv run pytest tests/test_search_units.py -k body` all green.
- [x] **5.3** Same file — mock vector invariant: `MOCK_QUERY_VECTOR` length 1024, L2 norm == 1.0.
      ✅ Criterion: the corresponding test is all green.
- [x] **5.4** Same file — `SearchService` orchestration (fake repo injection, does not touch OpenSearch/Bedrock): in mock mode, inject a fake repo that returns prepared knn/bm25 hits, and assert the fusion ranking, top-size truncation, `SearchResultItem` field mapping (`_id`→`mart_id`, `_source.martName`→`mart_name`, score=RRF score), and that both sides empty → `results==[]` without raising.
      ✅ Criterion: `uv run pytest tests/test_search_units.py` all green for the whole file, with conftest's `ANALYZER_MOCK_MODE=true` in effect and zero Bedrock calls.

## Phase 6 — mock-mode API smoke 🟠 (requires OpenSearch, not Bedrock)

- [x] **6.1** `tests/test_search_api_smoke.py`: collection-time ping of `localhost:9200`, unreachable → skipif for the whole module (aligned with the reachability pattern in `test_pipeline_e2e.py`, with the prerequisite command stated in the docstring).
      ✅ Criterion: when OpenSearch is stopped, `uv run pytest tests/test_search_api_smoke.py` shows skipped rather than failed.
- [x] **6.2** Same-file smoke assertions (`httpx.AsyncClient` + `ASGITransport`, mock mode):
      (a) `GET /search?q=robot vacuum` → 200, non-empty `results`, each entry contains `mart_id`/`mart_name`/`score`, scores descending;
      (b) **fusion evidence**: the results include products with a BM25 lexical hit (under the mock vector, k-NN is deterministic noise while BM25 is real — the results of a lexically strong query must contain a BM25 contribution);
      (c) boundaries: `size=101` → 422, `q=` (empty string) → 422, `size=1` → exactly 1 entry;
      (d) no-results query (e.g. `q=zzzzqqqqnonexistentterm`) → 200 + `results==[]` (**not 404**).
      ✅ Criterion: after starting OpenSearch, `uv run pytest tests/test_search_api_smoke.py` is all green; zero Bedrock calls during the test (`ANALYZER_MOCK_MODE=true`).
- [x] **6.3** Manual smoke (start the app with uvicorn, mock mode): `curl -s "localhost:8000/search?q=reishi health drink&size=5" | jq .`, visually confirm the JSON structure and 5 results.
      ✅ Criterion: curl returns 200 + valid `SearchResponse` JSON.

## Phase 7 — accuracy evaluation 🔴 (opt-in; real Bedrock + OpenSearch; **cost-disclosure gate before running**)

- [x] **7.1** Add `scripts/etl/judge_hybrid_search.py`: reuse `load_golden_set` (the approved gate still enforces exit 1) and the LLM-judge method from `judge_search_relevance.py` (Opus-class judge, `JUDGE_MODEL_ID` env overridable); against the running app (mock OFF), call `GET /search` to fetch the hybrid top-10, and **in the same run** place k-NN-only and BM25-only side by side (same judge, same batch, avoiding cross-run drift), outputting a three-column comparison to `out/search_eval_hybrid_{YYYYMMDD}.md` (design §10.3).
      ✅ Criterion: the script exists, with a module docstring stating the inputs/outputs/cost estimate/safety-disclosure requirement; when `meta.status != approved`, it exits 1 without issuing any external call.
- [x] **7.2** Run the evaluation (**gate: first disclose the estimated cost (order of magnitude < $1) to the user and obtain consent**; if the lab credentials have expired, run `scripts/refresh-lab-creds.sh`).
      ✅ Criterion: a cost-disclosure + consent record exists in the conversation; the report is produced.
- [x] **7.3** Determine the success criteria (report faithfully, do not loosen them):
      (a) global: hybrid relevant count ≥ max(vec-only relevant count, bm25-only relevant count);
      (b) complementarity preserved: for vector-strong queries (contextual, e.g. q11/q13) and BM25-strong queries (q04 ThinkPad), the hybrid relevant count is non-zero for both.
      ✅ Criterion: the report Summary explicitly lists both judgments and the data; if not met, mark ❌ faithfully and report to the user, without altering the criteria to fudge the numbers.

## Phase 8 — Verification (after all items complete)

- [x] **8.1** All tests green: `uv run pytest` (prerequisite: postgres + opensearch containers online; if not online, the corresponding modules skipping also counts as passing, but local acceptance should have both started).
      ✅ Criterion: 0 failed.
- [x] **8.2** **Zero-migration verification**: record the `alembic current` output before starting work, then run it again after completion with the same revision; `git status alembic/versions/` shows no new files.
      ✅ Criterion: revision is identical before and after, and `alembic/versions/` has no untracked files.
- [x] **8.3** Full boundary grep suite:
      - `grep -rn "HTTPException" src/recommender/search/` → 0 matches
      - `grep -rn "from recommender.config import settings" src/recommender/search/repository.py` → 0 matches (repo does not read settings)
      - `grep -rn "SearchRepository\|AsyncOpenSearch" src/recommender/search/router.py` → 0 matches (router does not cross layers)
      - `grep -rn "def get_search" src/recommender/` matches only `deps.py` (the single wiring point)
      - `grep -rn "normalize" src/recommender/search/embeddings.py` matches and is hardcoded `True` (not read from settings)
- [x] **8.4** Lifespan lifecycle verification: after starting uvicorn (mock mode) then Ctrl-C, the log has no aiohttp `Unclosed client session` warning (the client was `await close()`-d).
      ✅ Criterion: the shutdown log is clean.
- [x] **8.5** Documentation sync: `docs/plans/product-search-vectorization.md` §Phase 2 annotated with "specified as openspec/changes/product-search-hybrid-api"; `docs/architecture/architecture.md` adds a section on the `search/` domain module (including a record of the design decision "a deliberate exception to the layer-first codebase").
      ✅ Criterion: both locations grep for the string `product-search-hybrid-api`.
