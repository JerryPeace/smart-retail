# product-search-vectorization — Design

> Aligned with design decisions D1–D8 and work items P1-1 ~ P1-5 of `docs/plans/product-search-vectorization.md`. This document turns the approved decisions into an implementable spec; it does not reopen anything.

## 0. Design Overview

- **Simplicity First / POC scripts only**: the deliverables this round are docker service definitions + three `scripts/etl/` scripts + one golden set + pure-function tests. **No service / repository layer is built, and nothing is wired into FastAPI** — that is Phase 2's job (plan §5).
- **ETL First, LLM Last**: loading (P1-3) is pure algorithm; Bedrock only does embedding (an atomic operation, called directly via boto3, D7/D8), and the LLM is never used to parse or compute anything.
- **Idempotency and resumability are first-class citizens**: `_id=martId` (D5) makes re-running bulk index non-duplicating; embedding resume only fills the gaps. Lab credentials expire (1–12 hours), so "not finishing in one run" is expected behavior, not an error.
- **Script style aligns with the existing `scripts/etl/`** (`aggregate_monthly.py` etc.): the module docstring states input/output/usage, constants are centralized at the top of the file, pure functions are separated from IO, there is an `if __name__ == "__main__":` entry point, and it runs via `uv run python scripts/etl/xxx.py`.

## 1. P1-1 — Key points of the docker OpenSearch service definition

Add to `docker-compose.dev.yml` (aligned with the existing postgres/redis conventions for container_name, network, healthcheck):

| Item | Spec | Rationale |
|------|------|------|
| image | OpenSearch **2.19 series** (pin the latest patch tag on Docker Hub during implementation, e.g. `2.19.2`; `latest` is forbidden) | D1: 2.19 has faiss + RRF score-ranker (used in Phase 2) and avoids 3.x edge cases and the nmslib deprecation |
| smartcn plugin | **A small custom Dockerfile** (`docker/opensearch/Dockerfile`): `FROM opensearchproject/opensearch:<tag>` + `RUN bin/opensearch-plugin install --batch analysis-smartcn`, with compose pointing to it via `build:` | D6. The plugin version must exactly match the OpenSearch version; building it into the image is more deterministic than installing dynamically in the entrypoint and avoids reinstalling on restart |
| mode | `discovery.type=single-node` | 26k records is easy on a single node (vectors ~106MB) |
| security | `DISABLE_SECURITY_PLUGIN=true`, `DISABLE_INSTALL_DEMO_CONFIG=true` | **Local POC only** (see §7 safety); dropping certs/credentials keeps curl and opensearch-py connections as simple as possible |
| JVM | `OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g` | Plan §7: 1–2GB is enough |
| memlock | `bootstrap.memory_lock=true` + `ulimits.memlock: {soft: -1, hard: -1}` | OpenSearch's official recommendation, avoids swap |
| ports | `9200:9200` (5434/6380/4567/8081 are already taken; 9200 is free) | Verified against the current repo state |
| healthcheck | `curl -sf http://localhost:9200/_cluster/health \|\| exit 1`, `interval: 10s, timeout: 5s, retries: 12` (cold start is slow, so give ample retries) | Aligned with the existing services' healthcheck convention |
| volume | `opensearch_data:/usr/share/opensearch/data` | After loading 26k + vectors, we don't want to start over every time |
| container_name | `marketing-poc-opensearch`, attached to `app-network` | The existing naming convention |
| dashboards (optional) | `opensearchproject/opensearch-dashboards:<same tag>`, `5601:5601`, `DISABLE_SECURITY_DASHBOARDS_PLUGIN=true`, `OPENSEARCH_HOSTS=["http://opensearch:9200"]`, placed under `profiles: [dashboards]` | Aligned with the existing "optional services use profiles" convention (the api service follows the same pattern); off by default to save memory |

A Linux host needs `vm.max_map_count=262144`; Docker Desktop for Mac satisfies this out of the box (the local environment), so it is only noted in the README/docstring and not automated.

## 2. P1-2 — `products_v1` index settings / mapping

### settings

```jsonc
{
  "settings": {
    "index.knn": true,                // Must be set at index creation; cannot be changed live (see the index contract in the spec)
    "number_of_shards": 1,
    "number_of_replicas": 0,          // Single node; 0 both during loading and at steady state
    "refresh_interval": "-1"          // During loading only; restore to "1s" after P1-3 completes
  }
}
```

### mapping field table (the fields below are explicitly declared; the source's other fields use the dynamic default, not locked down for the POC)

| Field | type | analyzer / parameters | Description |
|------|------|----------------|------|
| `martId` | `keyword` | — | Product ID; also serves as `_id` (D5) |
| `martName` | `text` | `analyzer: smartcn` | Product name, a component of the embedding text (D4/D6) |
| `feature` | `text` | `analyzer: smartcn` | Product feature (contains HTML; the original goes into the index, cleaned only before embedding) |
| `keyword` | `text` | `analyzer: smartcn` | 7% null |
| `categoryLevel1Name` | `keyword` | — | Contains category contamination (brand name); kept as-is for the P1-5 demo |
| `categoryLevel2Name` | `keyword` | — | Same as above (marketing labels) |
| `categoryLevel3Name` | `keyword` | — | |
| `brand` | `keyword` | — | |
| `price` | `float` | — | |
| `isSearchable` | `integer` | — | Loading already filters to =1; the field is kept for verification |
| `embedding` | `knn_vector` | `dimension: 1024`, `method: {engine: "faiss", name: "hnsw", space_type: "innerproduct"}` | D2/D3: with Titan v2 `normalize:true`, the unit-vector innerproduct is equivalent to cosine |

The BM25 control group's (P1-5) `match` query hits `martName`/`feature`/`keyword` (smartcn tokenization), ensuring the control group is not a strawman dragged down by bad tokenization (D6: without smartcn, the standard analyzer splits Chinese character-by-character, making the comparison unfair).

How the index is created: the index is built directly inside the P1-3 loading script (`indices.create`, skipped if it already exists) — no separate "create-index script" is written, but the mapping/settings are written as **module-level constant dicts** at the top of `load_products_os.py` so they can be reviewed independently.

## 3. P1-3 — `scripts/etl/load_products_os.py`

```
Read products/OpenSearch_Full_20260612_030007.json (36MB single-line JSON array)
  → Structure probe: is the top-level first element a "product object", or a search-response hit containing _index/_id/_source?
  → Uniformly extract the source dict (pure function extract_sources(raw) → list[dict])
  → Filter isSearchable == 1 (expected to exclude 4 → 26,014)
  → opensearch-py helpers.bulk, action="index", _id=str(martId), chunk 500
  → After completion, restore refresh_interval="1s", refresh + _count verification
```

Design points:

- **Structure probing is a pure function** (`detect_format` / `extract_sources`): plan §2 already marked ⚠️ that we must first confirm whether the source is an array of `_source` objects or a search response — the script does not gamble on the format, accepts both, and fails fast on any other format (no LLM fallback; this is structured JSON, algorithm-first).
- **Idempotency**: `index` action + `_id=martId`, so re-running = overwriting the same doc and `_count` stays unchanged (D5; the load-idempotency contract in the spec).
- **A single `json.load` of 36MB into memory is acceptable** (POC, single machine); no streaming parser — Simplicity First.
- Connect with `OpenSearch(hosts=["http://localhost:9200"])`, host/port from top-of-file constants (no auth, security off).
- Print a summary at the end: total count, how many filtered out, bulk error count (>0 means exit 1).

## 4. P1-4 — `scripts/etl/embed_products_os.py`

```
Query OpenSearch: bool must_not exists "embedding" (resume mechanism: naturally only fills the gaps)
  → Fetch martId + the text fields needed for embedding (search_after / scroll pagination)
  → build_embed_text pure function assembles + cleans (D4)
  → ThreadPoolExecutor with 5–10 workers concurrently calls Bedrock invoke_model
  → Every 200–500 records, bulk update writes the embedding back
  → Loop until must_not exists returns no docs
```

Design points:

- **Bedrock client**: `boto3.Session(profile_name="lab", region_name="ap-northeast-1")` → `client("bedrock-runtime")`, model `amazon.titan-embed-text-v2:0` (verified working in plan §3). Each worker thread builds its own client (sharing a boto3 client across threads is risky; session-per-thread is the most robust).
- **Embedding-text assembly (D4, pure function `build_embed_text(doc) -> str`)**: `martName` + `feature` + `keyword` + the three-level `categoryLevelXName`, joined with newlines. Cleaning rules:
  - `feature` strip HTML (regex `<[^>]+>` → whitespace is enough; the POC does not pull in bs4)
  - Read each field with `doc.get(field) or ""` — **prevents `None` from being concatenated as the literal `"None"`** (specified in plan P1-4)
  - Collapse full-width / consecutive whitespace, strip
  - Truncate to **50,000 characters** (a conservative bound for Titan v2's 8,192 token / 50k char limit)
- **request body**: `{"inputText": text, "dimensions": 1024, "normalize": true}` — `normalize:true` is the precondition for D2's innerproduct≡cosine and **must not be omitted** (the embedding contract in the spec).
- **retry**: a hand-written exponential backoff (`base 1s, factor 2, max 8 attempts, with jitter`) for `ThrottlingException` / HTTP 429 / 5xx; other exceptions (e.g. ValidationException) fail fast without retry.
- **write-back**: `helpers.bulk` with the `update` action (`doc: {"embedding": [...]}`), batches of 200–500.
- **resume**: no progress file of its own is maintained — "no `embedding` field" is itself the progress state, so after an interruption, re-running the same command resumes. On credential expiry (`ExpiredTokenException`), print clear guidance: re-run after `./scripts/refresh-lab-creds.sh`.
- **concurrency**: 5–10 workers (top-of-file constant, default 8), respecting the Bedrock RPM quota; estimated ~1–1.5 hours serially, ~10–20 minutes concurrently.
- **cost**: ~3.9M tokens × Titan v2 ≈ **< $0.1 one-time**. The notification obligation before running is in §7.
- Print a summary at the end: records embedded this round, records still missing embedding (0 = done), retry count.

## 5. golden set + P1-5 verification

### 5.1 golden set file format

Path: `scripts/etl/golden_set_product_search.yaml` (**goes into git** — reused for the Phase 2 model benchmark, specified in plan P1-5).

```yaml
# Drafted by the agent from the 26k product data, effective after user review (review record below)
meta:
  status: draft          # draft → approved (changed once the user approves)
  approved_by: null      # filled with the user once approved
  approved_at: null
  source_file: OpenSearch_Full_20260612_030007.json
queries:
  - id: q01
    query: "lingzhi health drink"
    category: lexical_overlap        # surface-form overlap: BM25 should also work
    expected_mart_ids: ["123456"]    # martId actually pulled from the source data
    rationale: "surface form directly matches the Grape King Lingzhi King product name"
  - id: q11
    query: "a drink that boosts immunity"
    category: non_overlap            # no surface-form overlap: BM25 should fail, vectors should succeed
    expected_mart_ids: ["123456", "234567"]
    rationale: "semantically corresponds to lingzhi/ginseng beverages; the product name and feature contain no word for 'immunity' (verified by grep)"
```

Spec: 15–20 queries; both classes present, **`non_overlap` ≥ 8** (the denominator of the success criterion is this class); `expected_mart_ids` must be martIds that actually exist in the source file (verified with grep/jq when drafting, never fabricated); each has a `rationale` for the user's review judgment.

### 5.2 Drafting and review process (settled: agent drafts, user reviews)

1. The agent reads the source JSON, picks representative products (including ≥2 that land on category-contaminated products, such as Grape King Lingzhi King), and drafts the YAML (`status: draft`).
2. **User review gate**: the user goes through each query to judge whether it looks like a real query and whether expected_mart_ids is reasonable, and may add/remove/edit. After approval, `status: approved`.
3. **When `status != approved`, `verify_search_os.py` immediately exits 1 and refuses to run** — the gate is enforced by code, not by good faith.

### 5.3 P1-5 — `scripts/etl/verify_search_os.py`

For each query:

1. Embed the query with the same Titan v2 + `normalize:true` (**query/doc use the same model, dimension, and normalize**, the embedding contract in the spec; the ~20 calls' cost is negligible, but it is still real Bedrock, folded into the §7 notification).
2. **k-NN query**: `{"knn": {"embedding": {"vector": [...], "k": 10}}}` takes top-10.
3. **BM25 control group**: `multi_match` hits `martName`/`feature`/`keyword`, takes top-10.
4. Against `expected_mart_ids`, compute hit@10 for each side.
5. **Category-contamination demo** (always run additionally): a "health supplements" query + `categoryLevel1Name` filter (misses Grape King — category=brand name) vs pure vectors (finds it).

### 5.4 Output format

Write `out/search_eval_{YYYYMMDD}.md` (following the `out/` convention):

```markdown
## q11 "a drink that boosts immunity" (non_overlap)
| rank | vector top-10           | BM25 top-10 |
|------|----------------------|-------------|
| 1    | ✅ 123456 Grape King Lingzhi King | 987654 unrelated product |
...
Verdict: vector hit@10 = 2/2, bm25 hit@10 = 0/2 → **vector-only win**

## Summary
- non_overlap, N total: vector-only wins = X (success criterion ≥ 3)
- lexical_overlap, M total: BM25 hit rate (sanity check, both sides should work)
- category-contamination demo: category filter misses K / found by vectors
```

**Success criterion (quantitative)**: among the `non_overlap` class, queries with "vector hit@10 ≥1 and BM25 hit@10 = 0" **≥ 3** (the plan writes "≥ N" without a value; this design takes N=3, adjustable together with the user during golden-set review). Also require that the `lexical_overlap` class's BM25 hit rate is normal (proving the control group is not a strawman).

## 6. Test strategy (settled: test only pure functions)

Add `tests/test_product_search_units.py`, aligned with the `tests/test_etl_units.py` convention (module docstring notes no DB / no network / no Docker).

| Pure function | Located in | What is tested |
|--------|------|------|
| `detect_format` / `extract_sources` | `load_products_os.py` | Both JSON structures (plain array vs search-response hits) resolve to a source list; unknown format raises |
| `build_embed_text` | `embed_products_os.py` | Field assembly order, `keyword=None` → `""` (the literal `"None"` does not appear), strip HTML, whitespace collapsing, 50k truncate |
| `strip_html` / `truncate` and similar sub-functions | `embed_products_os.py` | Boundaries: empty string, pure HTML, over-long input |
| golden set loader / `status==approved` gate judgment | `verify_search_os.py` | YAML schema validation, draft refuse-to-run judgment |

- **Import method**: scripts are not a package, so tests load script modules via `importlib.util.spec_from_file_location` (the scripts must have an `if __name__ == "__main__":` guard so importing does not trigger IO).
- **Not tested**: OpenSearch I/O, Bedrock I/O, retry/concurrency behavior (I/O verification goes through the curl/_count manual criteria in tasks; settled).

## 7. Safety (aligned with `.claude/rules/safety.md`)

| Risk | Countermeasure |
|------|------|
| **P1-4 hits real Bedrock and costs money** | Estimated ~3.9M tokens ≈ **< $0.1 one-time**. Although far below the "batch 100+ prompts" threshold, it is still real money and a real call: **before running `embed_products_os.py`, the user must be explicitly notified of the estimated cost and consent obtained** (tasks set a gate). P1-5's ~20 query embeddings are folded into the same notification |
| **lab credentials expire (1–12 hours)** | embed not finishing in one run is expected behavior. The script catches `ExpiredTokenException` → prints the `./scripts/refresh-lab-creds.sh` guidance → re-running resumes (the must_not exists mechanism). Do not print AWS keys in logs / reports |
| **local security off** | `DISABLE_SECURITY_PLUGIN=true` is **local POC only**, made explicit in a docker-compose comment; any discussion of going to prod returns to the migration assessment in plan §7 |
| **36MB source file** | Kept out of git: add `products/OpenSearch_Full_*.json` to `.gitignore`. The product catalog is not PII, but it is still internal company data and must not leak |
| **local OpenSearch data** | Pure local docker volume; delete / rebuild is safe (equivalent to the LocalStack level); `docker compose down -v` must still be warned about first (it would also drop the postgres dev data — the existing safety rule) |
| **zero DB / migration impact** | This round does not touch PostgreSQL and produces no alembic migration; `alembic current` is consistent before and after |
