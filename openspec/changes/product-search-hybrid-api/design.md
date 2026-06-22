# product-search-hybrid-api — Design

## 0. Design Overview

- **Simplicity First**: this round delivers only the minimal set of the "core hybrid search API". No filters, pagination, demotion, caching, or fallback retries — those wait for real demand.
- **Reuse, don't rewrite**: the k-NN / BM25 query DSL and the query embedding logic are **lifted into the search module and converted to async** from the reusable functions in `scripts/etl/verify_search_os.py` (that script's docstring already foreshadows this). The golden set and the LLM-judge scale are reused directly from the Phase 1 deliverables.
- **Algorithm handles fusion, LLM only does embedding**: RRF is a pure Python formula (`Σ 1/(k+rank)`); no OpenSearch pipeline or LLM is involved in ranking. Bedrock does exactly one atomic thing in this chain: query embedding.
- **Mock by default**: under `analyzer_mock_mode=true` (the existing default), `/search` runs end to end with zero Bedrock calls and zero cost. Real embedding is opt-in.
- **Zero migration**: search's storage layer is OpenSearch; not a single character of the PostgreSQL schema is touched.

## 1. Domain Module `src/recommender/search/` — Structure and Responsibilities

### 1.1 Why a domain module (a deliberate exception in a "layer-first" codebase)

Plan §Phase 2 already locked it in: search's infrastructure (OpenSearch) is completely different from the core Postgres + Bedrock chain, making it the codebase's **first infra-boundary domain**. Scattering its repository/service/router into `repositories/` `services/` `api/` would blur the boundary of "who depends on OpenSearch"; only by enclosing it as a self-contained `search/` module can it be decoupled and made easy to swap out / deploy independently.

**How it coexists with the existing architecture** (a domain module is not a parallel universe; the three-layer discipline and cross-cutting infrastructure are unchanged):

- **Inside** the module it is still router → service → repository three layers, with responsibilities aligned to coding-rules (router never touches the client, service never returns raw hits, repository is pure I/O with no business judgment).
- **DI wiring still lives only in `deps.py`** (the single wiring point gets no exception, see §9 trade-off).
- **Error handling follows the existing cross-cutting path**: the search module does not raise `HTTPException` itself; no results is a normal business outcome (returns an empty list), not a `NotFoundError`; unexpected errors like OpenSearch connection failures bubble straight up to the global Exception handler in `main.py` to become a 500.
- **lifespan integrates into `main.py`'s** existing startup/shutdown flow; no separate process or background daemon.

### 1.2 File structure

```
src/recommender/search/
├── __init__.py
├── schemas.py       # Pydantic DTOs: SearchResultItem / SearchResponse
├── embeddings.py    # @lru_cache get_bedrock_embeddings(...) (following llm.py) + MOCK_QUERY_VECTOR
├── client.py        # @lru_cache get_opensearch_client() + async close_opensearch_client()
├── repository.py    # SearchRepository: build_knn_body/build_bm25_body pure functions + hybrid_msearch I/O
├── rrf.py           # reciprocal_rank_fusion pure function
├── service.py       # SearchService: embed → msearch → RRF → DTO
└── router.py        # GET /search
```

| File | Responsibility | What it does not do |
|------|------|---------|
| `schemas.py` | `SearchResultItem` (`mart_id` / `mart_name` / `score` / `brand` / `price` / `category`, the last three optional), `SearchResponse` (`query` + `results: list[SearchResultItem]`) | Does not hold the OpenSearch hit structure (that's repository-internal) |
| `embeddings.py` | `get_bedrock_embeddings(model_id, region, profile, dimensions)` returning a cached `BedrockEmbeddings`; the `MOCK_QUERY_VECTOR` constant | Does not decide mock (that's the service's job); does not hold the chat model (that's `llm.py`) |
| `client.py` | Construction and shutdown of the AsyncOpenSearch singleton | Does not issue queries |
| `repository.py` | DSL construction (pure functions) + `msearch` calls (I/O), returning two sets of raw hits | Does not read `settings` (host/index are injected via the constructor); does no fusion, no DTO conversion |
| `rrf.py` | Pure-function rank fusion | Zero imports of OpenSearch / Pydantic types |
| `service.py` | Orchestration: mock decision, embed, candidate count, fusion, hit→DTO mapping | Does not assemble DSL, does not touch HTTP |
| `router.py` | Parameter validation, calling the service, returning `SearchResponse` | Does not inject the repository, does not raise `HTTPException` |

## 2. New Settings Fields (`config.py`)

```python
# === OpenSearch (local docker, vectors loaded in Phase 1) ===
opensearch_host: str = "http://localhost:9200"
opensearch_index: str = "products_v1"

# === Bedrock Embedding (separate from the LLM section: different model/region) ===
bedrock_embed_model_id: str = "amazon.titan-embed-text-v2:0"
bedrock_embed_region: str = "ap-northeast-1"   # Titan in the Tokyo lab; LLM uses us-east-1
embed_dimensions: int = 1024
```

| Field | Default | Notes |
|------|------|------|
| `opensearch_host` | `http://localhost:9200` | Local docker, security off, no auth |
| `opensearch_index` | `products_v1` | The k-NN index built in Phase 1 |
| `bedrock_embed_model_id` | `amazon.titan-embed-text-v2:0` | **Must be the same model as the doc-side embedding** (§3 invariant) |
| `bedrock_embed_region` | `ap-northeast-1` | Phase 1 verified Titan v2 works in Tokyo (lab profile) |
| `embed_dimensions` | `1024` | **Must be the same dimension as the doc side** (§3 invariant) |

The mock path reuses the existing `analyzer_mock_mode` (default `true`); no new flag is added — "don't call real LLM/embedding in local dev" is the same switch semantics. The AWS profile reuses the existing `aws_profile` (lab).

## 3. Invariant: query and doc must use the same model, same parameters, same dimensions

Phase 1 embedded the 26,014 products with `amazon.titan-embed-text-v2:0`, `dimensions=1024`, `normalize=true`, and indexed the `embedding` field as faiss/hnsw/**innerproduct**. innerproduct is equivalent to cosine only if **both sides are unit vectors**. Therefore:

> **Any query embedding must be exactly identical to the doc side: same model (Titan v2), same `dimensions=1024`, same `normalize=true`.** If any parameter differs, the two vectors live in different spaces and the k-NN scores are meaningless — this is not a quality regression, it's silently all-wrong.

How it's enforced:

- `BedrockEmbeddings` is constructed with `model_kwargs={"dimensions": settings.embed_dimensions, "normalize": True}` (Titan v2 request body parameters, equivalent to the boto3 body of Phase 1's `embed_query`; during implementation, verify the returned length is 1024 with one real call).
- `normalize=True` is **hardcoded inside the builder** and not made a Setting — it is not a tunable parameter, it is a space-consistency invariant; making it configurable leaves a "set it wrong and everything is silently wrong" landmine.
- The mock vector must likewise be a 1024-dim **unit vector** (§5), to keep innerproduct valid.
- A future embedding model change (after the Phase 2b benchmark) = full re-embed + new index (`products_v2` + reindex/alias), not just changing one Setting — this fact is written into the spec's embedding contract.

## 4. Embeddings builder (`search/embeddings.py`)

Following `llm.py`'s `@lru_cache` pattern (process-level cache, shared across requests; construction is synchronous with no await point, so it is naturally race-free):

```python
@lru_cache(maxsize=4)
def get_bedrock_embeddings(
    model_id: str,
    region: str,
    profile: str | None,
    dimensions: int,
):
    from langchain_aws import BedrockEmbeddings

    return BedrockEmbeddings(
        model_id=model_id,
        region_name=region,
        credentials_profile_name=profile,
        model_kwargs={"dimensions": dimensions, "normalize": True},
    )
```

- Placed in `search/embeddings.py` rather than `llm.py`: the only consumer today is search; `llm.py` manages chat models (ChatBedrockConverse), which is semantically different. If chains later need embeddings too, promote it to the top level (a one-time import-path change).
- The service calls via **`aembed_query`** (the async interface of LangChain's Embeddings base class, which wraps the synchronous boto3 call in an executor underneath) — it does not run synchronous `embed_query` directly on the event loop (§7 async contract).
- lifespan preheats it when not in mock mode (`_preheat_embeddings()`, following `_preheat_llm`'s best-effort try/except: a failure only logs a warning and does not block startup).

## 5. Mock path design

```python
# search/embeddings.py
MOCK_QUERY_VECTOR: list[float] = [1.0] + [0.0] * 1023   # 1024-dim unit vector
```

- **The decision point is in the service** (aligned with `AgentService`'s existing pattern: `__init__` reads `settings.analyzer_mock_mode`, methods branch on it): in mock mode, `_embed_query()` returns `MOCK_QUERY_VECTOR` directly, with zero network and zero credential requirements.
- **Design trade-off — a fixed vector rather than a hash-based pseudo-semantic vector**: the point of mock is to "verify the pipeline runs" (embed → msearch → RRF → DTO), not to verify semantic quality. A fixed unit vector lets k-NN run legally (innerproduct is valid for unit vectors) and returns deterministic results (always the same batch of products closest to that vector), but it is **semantically meaningless** — this is a stated limitation, not a bug. A hash-based pseudo-vector would mislead people into thinking the mock results have semantic relevance, which is actively harmful.
- In mock mode the **BM25 half is fully real** (no Bedrock needed), so the mock-mode smoke test can still verify that "RRF really fused two non-empty lists" — the BM25 side's results are real, while the k-NN side is deterministic noise.
- Real accuracy evaluation (golden set + judge) requires mock OFF + real Titan + real OpenSearch, and is opt-in (§8.3).

## 6. Repository (`search/repository.py`)

### 6.1 Pure-function DSL builders (lifted from verify_search_os.py)

```python
def build_knn_body(vector: list[float], k: int) -> dict:
    return {"size": k, "query": {"knn": {"embedding": {"vector": vector, "k": k}}}}

def build_bm25_body(query_text: str, k: int) -> dict:
    return {
        "size": k,
        "query": {
            "multi_match": {
                "query": query_text,
                "fields": ["martName", "feature", "keyword"],   # same as Phase 1, smartcn tokenization
            }
        },
    }
```

Pure functions, zero I/O — unit tests can assert the dict structure directly, without OpenSearch.

### 6.2 hybrid msearch (I/O, separated from the builders)

```python
class SearchRepository:
    def __init__(self, os_client: AsyncOpenSearch, index: str) -> None: ...

    async def hybrid_msearch(
        self, vector: list[float], query_text: str, k: int
    ) -> tuple[list[dict], list[dict]]:
        """One msearch runs k-NN and BM25 concurrently, returning (knn_hits, bm25_hits) as two sets of raw hits."""
        body = [
            {"index": self._index}, build_knn_body(vector, k),
            {"index": self._index}, build_bm25_body(query_text, k),
        ]
        resp = await self._client.msearch(body=body)
        # resp["responses"] corresponds in order to the two queries; if either side contains an error → raise (fail fast, 500 via the global handler)
```

- **`msearch` concurrency semantics**: multi-search is one round-trip to OpenSearch where the server runs the multiple queries in parallel — no app-side `asyncio.gather` over two connections is needed. The body is an interleaved list of "header dict + query dict" with NDJSON semantics; opensearch-py accepts a list[dict] and serializes it automatically.
- **Fail fast on partial failure**: a per-response error in msearch (e.g. a DSL error in one query) → raise directly and let the global handler return 500. Single-side degradation (falling back to pure BM25 when k-NN is down) is resilience design, belongs to Phase 2b, and is not done this round (Simplicity First).
- The repository **does not read `settings`**: `os_client` and `index` are injected by deps.py (aligned with the "repo reads global settings" anti-pattern that architecture-convergence eliminated).

## 7. RRF pure function (`search/rrf.py`)

```python
def reciprocal_rank_fusion(
    result_lists: Sequence[Sequence[str]],   # each element is a "sorted list of doc ids"
    k: int = 60,
) -> list[tuple[str, float]]:
    """score(doc) = Σ_lists 1/(k + rank), with rank starting at 1.

    Returns (doc_id, score) sorted by descending score; ties broken by lexicographic doc_id order (deterministic).
    Empty lists / missing on one side are both valid: an absent list simply contributes no score.
    """
```

Settled interface decisions:

- **Takes `Sequence[Sequence[str]]` (lists of doc ids) rather than raw hits** — pure data in and out, zero coupling to OpenSearch types; a unit test can feed `[["a","b"],["b","c"]]` to verify fusion correctness. Hit metadata (martName, price…) is preserved by the service as an `_id → _source` dict and joined back after fusion.
- **A variable-length list** rather than two fixed parameters — if Phase 2b adds a third signal path (e.g. a re-ranked list after category demotion), the interface need not change.
- `k=60` is the default value from the original RRF paper and industry convention; it is exposed as a parameter but not opened up to the API query string this round (no need to expose it before there is a tuning requirement).
- **Deterministic tie-break**: ties are sorted by doc_id — reproducible tests, stable online results.

## 8. Service and Router

### 8.1 SearchService (`search/service.py`)

```python
class SearchService:
    def __init__(self, repo: SearchRepository) -> None:
        self._repo = repo
        self.mock_mode = settings.analyzer_mock_mode   # the service layer reading config is legal

    async def search(self, query: str, size: int = 10) -> SearchResponse:
        vector = await self._embed_query(query)            # mock → MOCK_QUERY_VECTOR
        candidate_k = 2 * size                              # candidate window per side
        knn_hits, bm25_hits = await self._repo.hybrid_msearch(vector, query, candidate_k)
        fused = reciprocal_rank_fusion([ids(knn_hits), ids(bm25_hits)])   # k=60
        # take top-size → join metadata back via the _id→_source map → SearchResultItem(score=RRF score)
        return SearchResponse(query=query, results=items)
```

- **Candidate window `candidate_k = 2 * size`** (default size=10 → 20 per side): RRF fusion needs a per-side window wider than the final size, otherwise good results in the middle of both sides (like "#11 in list A + #11 in list B") get cut before fusion. 2× is the minimal sufficient multiplier; for a 26k-document index, taking 200 (when the size cap is 100) is no strain at all. Not made a Setting — there is no tuning requirement.
- **No results returns an empty `results: []` with HTTP 200** — "a search that misses" is a normal business outcome, not an error, semantically different from `GET /recommendations/{id}` returning 404 when a specific resource is not found.
- `SearchResultItem.score` holds the **RRF fusion score** (not the OpenSearch _score — the two sides' _score values have different scales and are inherently incomparable, which is one of the reasons for using RRF).
- The service returns a Pydantic DTO and **does not return raw hit dicts** (aligned with the "service does not return ORM/raw structures" discipline).

### 8.2 Router (`search/router.py`)

```python
router = APIRouter(prefix="/search", tags=["search"])

@router.get("", response_model=SearchResponse)
async def search(
    service: SearchServiceDep,
    q: str = Query(min_length=1),
    size: int = Query(default=10, ge=1, le=100),   # aligned with the recommendations cap convention
):
    return await service.search(q, size=size)
```

No try/except, no raising `HTTPException` — unexpected errors bubble to the global handler (aligned with the API-layer discipline after the architecture-convergence consolidation).

## 9. App integration: client lifecycle / deps / lifespan

### 9.1 AsyncOpenSearch client lifecycle (`search/client.py`)

```python
@lru_cache(maxsize=1)
def get_opensearch_client() -> AsyncOpenSearch:
    from opensearchpy import AsyncOpenSearch
    return AsyncOpenSearch(hosts=[settings.opensearch_host])   # local security off, no auth/TLS

async def close_opensearch_client() -> None:
    """Called by lifespan shutdown: close the aiohttp session, then clear the cache."""
    # only close if the cache has a value; cache_clear() after close, to avoid a lingering closed client
```

- **Follows `llm.py`'s lru_cache pattern rather than `app.state`**: (a) consistent with the existing codebase (one singleton pattern is enough); (b) construction is synchronous with no await point, so it is naturally race-free; (c) the deps.py provider calls the function directly, without reaching into `app.state` from the request. The cost is that shutdown needs an explicit close + cache_clear — consolidated into a single `close_opensearch_client()`.
- AsyncOpenSearch construction **issues no network connection** (lazy connect); building it at startup just readies the object; the real connection happens on the first query.
- **Must `await client.close()`**: AsyncOpenSearch is backed by an aiohttp session, and not closing it emits an unclosed-session warning at shutdown (close is a coroutine).

### 9.2 lifespan (`main.py`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    _preheat_llm()
    get_opensearch_client()        # build the client object (lazy connect, does not block startup)
    _preheat_embeddings()          # when not mock, best-effort build BedrockEmbeddings (following _preheat_llm)
    yield
    await close_opensearch_client()   # Shutdown: close the aiohttp session
```

The existing "shutdown has nothing to do at the POC stage" comment is updated accordingly — this is the app's first resource that needs shutdown cleanup.

### 9.3 deps.py (the single wiring point)

```python
def get_os_client() -> AsyncOpenSearch:
    return get_opensearch_client()                    # share the process singleton

OSClientDep = Annotated[AsyncOpenSearch, Depends(get_os_client)]

def get_search_repository(os_client: OSClientDep) -> SearchRepository:
    return SearchRepository(os_client, index=settings.opensearch_index)

SearchRepoDep = Annotated[SearchRepository, Depends(get_search_repository)]

def get_search_service(repo: SearchRepoDep) -> SearchService:
    return SearchService(repo)

SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
```

**Trade-off — centralized wiring in deps.py vs co-locating into search/**: the purist view of a "self-contained" domain module would put the providers in `search/deps.py`. Decision: **centralize in `deps.py`** — coding-rules and architecture-convergence only just established "deps.py is the single wiring point", and making an exception for the very first new module would render the rule meaningless in name only; there are only three provider functions, so the cost of centralizing approaches zero, while the value of "seeing the whole app's DI at a glance" is concrete. The search module retains "business logic self-contained" and cedes "wiring self-contained".

## 10. Testing strategy

### 10.1 Unit (CI, no docker, no network) — `tests/test_search_units.py`

| Target | Assertions |
|------|------|
| `reciprocal_rank_fusion` | Correctness of dual-list fusion (hand-compute 1/(60+rank) to verify score and order); a doc in the intersection has its scores summed and ranks higher; the effect of the `k` parameter; empty lists; missing on one side; deterministic tie-break |
| `build_knn_body` / `build_bm25_body` | dict structure, `size`, field list (martName/feature/keyword), the vector carried through verbatim |
| `MOCK_QUERY_VECTOR` | length 1024, L2 norm == 1.0 (unit-vector invariant) |
| `SearchService` orchestration | Inject a fake repo (returning two sets of canned hits) + mock mode: verify fusion → top-size → DTO mapping, empty result returns an empty list |

### 10.2 mock-mode API smoke (needs OpenSearch, not Bedrock) — `tests/test_search_api_smoke.py`

- Prerequisite: `docker compose -f docker-compose.dev.yml --env-file .env.local up -d opensearch` (the Phase 1 volume already contains 26,014 records + vectors). If OpenSearch `localhost:9200` is unreachable → the whole module is `pytest.mark.skipif`-skipped (aligned with the DB reachability pattern of `test_pipeline_e2e.py`).
- conftest already forces `ANALYZER_MOCK_MODE=true` → zero Bedrock.
- Assertions: `GET /search?q=robot vacuum` returns 200, `results` is non-empty, each item has `mart_id`/`mart_name`/`score`, score is descending; **fusion evidence**: results contain BM25 lexical hits (under the mock vector the k-NN side is noise and the BM25 side is real, so a lexically strong query must have a BM25 contribution); `size` boundaries (`size=101` returns 422, empty `q=` returns 422); a no-result query returns 200 + `[]`.

### 10.3 Accuracy evaluation (opt-in, real Bedrock + OpenSearch) — `scripts/etl/judge_hybrid_search.py`

- Reuse the Phase 1 scale: golden set (15 approved queries, the approved gate still programmatically enforced) + the LLM-judge method of `judge_search_relevance.py` (Opus-class judge, as the Phase 1 conclusion recommended).
- Against the **running app** (mock OFF), hit `GET /search` and take the hybrid top-10; in the **same round**, re-run k-NN-only and BM25-only side by side (same judge, same batch, to avoid cross-round judge drift), and compare in three columns.
- Success criterion: **hybrid is no worse than any single method** — globally, hybrid relevant count ≥ max(vec relevant count, bm25 relevant count), and neither vector-strength queries (q11-style situational ones) nor BM25-strength queries (q04 ThinkPad) may drop hybrid to zero (complementarity must be preserved, not averaged away). If unmet, report it faithfully per the Phase 1 convention, without loosening the judgment.
- **Money gate**: 15 query embeddings + three paths × ~10 products × judge ≈ a few hundred Haiku/Opus calls, on the order of < $1, but still real Bedrock — before running, you must inform the user and obtain consent (safety.md §1); not CI, not default.

## 11. Key design trade-offs

| Trade-off | Decision | Rationale |
|------|------|------|
| ⭐ Fusion: application-side Python RRF vs OpenSearch native search pipeline (score-ranker-processor, available in 2.19) | **Application-side Python RRF** | (a) A pure function is unit-testable: `Σ 1/(k+rank)` is under ten lines, fully covered by pytest; a pipeline's fusion behavior can only be integration-tested. (b) Reuse the POC: the knn/bm25 query DSL is lifted directly from the verify script, whereas a pipeline would require rewriting into a hybrid query + pipeline config file. (c) No new OpenSearch-side state (a pipeline is a cluster resource that must be created/version-controlled/migrated). (d) 26k documents, ≤200 candidates per side, app-side fusion costs microseconds. Cost: giving up the server-side single-query round-trip optimization — recovered by using msearch's one round-trip |
| ⭐ Domain module `search/` vs scattering into existing `repositories/` `services/` `api/` | **Domain module** (locked in plan §Phase 2) | search is the first infra-boundary domain (OpenSearch ≠ the Postgres+Bedrock core chain); enclosing it makes the boundary visible and independently swappable. Inside, the module still follows the three-layer discipline + centralized wiring in deps.py (§1.1, §9.3), it is not extraterritorial |
| ⭐ AsyncOpenSearch vs synchronous OpenSearch client | **AsyncOpenSearch** (`opensearch-py[async]`, aiohttp) | The app is fully async (FastAPI + asyncpg + aioboto3); a synchronous client would block the event loop, where one slow query stalls all in-flight requests. This is exactly the installation method of the official async guide |
| ⭐ client lifecycle: lru_cache module singleton vs `app.state` | **lru_cache + explicit close in lifespan** (§9.1) | Consistent with `llm.py`'s existing pattern; the deps provider need not reach into the request; the cost (explicit close + cache_clear at shutdown) is consolidated into a single function |
| ⭐ mock vector: fixed unit vector vs hash-based pseudo-semantic vector | **Fixed `[1.0, 0…]`** | Mock verifies the pipeline, not semantics; a fixed vector is deterministic and keeps innerproduct valid; a pseudo-semantic vector is over-engineering and misleading (§5) |
| ⭐ RRF interface: take doc id lists vs take raw hits | **`Sequence[Sequence[str]]` → `list[(id, score)]`** | Zero OpenSearch coupling, tests can feed string lists; metadata join is the service's mapping responsibility (§7) |
| Two-query concurrency: one `msearch` round-trip vs two `search` calls via `asyncio.gather` | **msearch** | One network round-trip, server-side parallelism; gathering two connections offers no extra benefit and doubles the connection overhead |
| msearch single-side failure: fail fast vs degrade to single path | **fail fast → 500** | Degradation is a resilience feature no one asked for; silent degradation would also let accuracy quietly drop unnoticed (better a 500 that is seen). Revisit in Phase 2b |
| embeddings builder location: `search/embeddings.py` vs next to `llm.py` | **Inside the search module** | The only consumer is search; `llm.py`'s semantics are the chat model. Promote it when a second consumer appears |
| Make `normalize` a Setting? | **No, hardcode True** | It is a space-consistency invariant, not a parameter (§3); making it configurable leaves a silently-all-wrong landmine |
| Candidate window `candidate_k` | **`2 * size`, hardcoded** | The minimal sufficient multiplier for the fusion window; no tuning requirement, so not put in Settings (§8.1) |
| Expose RRF `k=60` to the API? | **Function parameter yes, API no** | An industry-convention default; exposing a tuning knob in the query string is surface area with no demand |
