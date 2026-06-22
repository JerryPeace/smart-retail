# Spec: product-search-vectorization

This spec defines the six contracts that must hold once Phase 1 (P1-1 ~ P1-5) is complete: the index contract, the load-idempotency contract, the embedding contract, the verification contract, the safety contract, and the testing contract. After implementation, any artifact that violates a Requirement below is considered to have failed acceptance. Aligned with decisions D1–D8 in `docs/plans/product-search-vectorization.md`.

## ADDED Requirements

### Requirement: Index contract — get the `products_v1` mapping right the first time; knn cannot be hot-changed

`products_v1` SHALL be created with `index.knn=true`; the `embedding` field SHALL be a `knn_vector` with `dimension=1024` and `method={engine: faiss, name: hnsw, space_type: innerproduct}` (D1/D2/D3); `martName`/`feature`/`keyword` SHALL be `text` with `analyzer=smartcn` (D6); `categoryLevelXName`/`brand` are `keyword`, `price` is `float`, `isSearchable` is `integer`. Because `index.knn` and a `knn_vector` mapping cannot be hot-changed on an existing index, any mapping change SHALL go through "create a new index (e.g. `products_v2`) + reindex + alias switch" and SHALL NOT attempt an in-place modification.

#### Scenario: mapping acceptance
- **WHEN** running `curl -s localhost:9200/products_v1/_mapping`
- **THEN** `embedding` shows `dimension: 1024`, `engine: "faiss"`, `name: "hnsw"`, `space_type: "innerproduct"`, and the analyzer for `martName`/`feature`/`keyword` is `smartcn`

#### Scenario: smartcn segmentation is active
- **WHEN** the text "靈芝保健飲品" is analyzed via the `_analyze` API with the `smartcn` analyzer
- **THEN** it produces word-level tokens (e.g. "靈芝", "保健"), not character-by-character — ensuring the BM25 control group (P1-5) is not a straw man crippled by bad segmentation

#### Scenario: when the mapping needs to change
- **WHEN** it turns out the `embedding` dimension, engine, or any field type needs to change
- **THEN** create a new index version + `_reindex` + alias switch; do not modify the `products_v1` mapping in place

### Requirement: Load-idempotency contract — rerunning leaves `_count` unchanged

`load_products_os.py` SHALL load using `_id=str(martId)` (D5) and the bulk `index` action, so that a rerun overwrites rather than adds; it SHALL filter out products with `isSearchable != 1` (4 records expected to be excluded); it SHALL first probe the structure of the source JSON (both plain array and search-response hits are supported), and on an unknown structure it SHALL fail fast with an error rather than guess or fall back to an LLM (structured JSON falls within the algorithmic domain). After loading completes it SHALL restore `refresh_interval`.

#### Scenario: idempotent rerun
- **WHEN** `uv run python scripts/etl/load_products_os.py` is run twice in a row
- **THEN** after both runs `GET products_v1/_count` is exactly the same (≈26,014) and the document count does not double

#### Scenario: filter non-searchable products
- **WHEN** the source file contains products with `isSearchable=0`
- **THEN** those products do not appear in the index, and the filtered count is printed in the summary log

#### Scenario: unknown JSON structure
- **WHEN** the source file's top-level structure is neither an array of product objects nor a search-response containing `_source`
- **THEN** the script terminates with a non-zero exit code and prints the structure it actually detected, writing no documents

### Requirement: Embedding contract — normalize:true, same model for query/doc, resume only backfills the missing

All embeddings SHALL be produced by `amazon.titan-embed-text-v2:0`; the request body SHALL include `"dimensions": 1024` and `"normalize": true` — normalize is the prerequisite for innerproduct being equivalent to cosine (D2) and SHALL NOT be omitted. The query-side embedding in P1-5 SHALL use exactly the same model, dimension, and normalize setting as the document side. The embedding text SHALL be assembled by the `build_embed_text` pure function (D4: martName+feature+keyword+three-level categoryName), with cleaning rules: strip HTML, handle empty field values with `or ""` (the output must not contain the literal `"None"`), and truncate to 50,000 characters. `embed_products_os.py` SHALL use "the `embedding` field does not exist" as its sole progress state: a rerun only backfills the missing, does not re-embed completed documents, and maintains no extra progress file.

#### Scenario: full coverage
- **WHEN** the embed script (possibly across multiple resume runs) finally completes
- **THEN** the `exists embedding` `_count` equals the total index `_count`, and a randomly spot-checked document's `embedding` length is 1024

#### Scenario: interrupted resume
- **WHEN** embedding is interrupted midway (Ctrl-C or lab credentials expiring) and the same command is rerun
- **THEN** the script processes only the documents still missing `embedding`, the log's per-round processed count is less than the total, and documents that already have vectors are not re-sent to Bedrock

#### Scenario: empty fields do not pollute the embedding text
- **WHEN** a product's `keyword` is null / a missing field (about 7% of the source)
- **THEN** `build_embed_text`'s output contains no literal string `"None"`, and that field is skipped as an empty string

#### Scenario: rate-limit retry
- **WHEN** Bedrock returns ThrottlingException / 429 / 5xx
- **THEN** retry with exponential backoff (up to 8 times); non-transient errors such as ValidationException fail fast without retry

### Requirement: Verification contract — golden set with two query categories + a quantified success threshold

The golden set (`scripts/etl/golden_set_product_search.yaml`) SHALL contain 15–20 queries, split into `lexical_overlap` (lexical overlap, where BM25 should also do well) and `non_overlap` (no lexical overlap, where BM25 should fail and vectors should succeed), with non_overlap ≥ 8 entries; each SHALL contain `query`/`category`/`expected_mart_ids`/`rationale`, and `expected_mart_ids` SHALL be martIds that actually exist in the source file. The golden set SHALL be drafted by the agent (`meta.status: draft`) and may be used for verification only after **user review approval** (`status: approved`) — `verify_search_os.py` SHALL refuse to run with a non-zero exit code when `status != approved`. Verification SHALL, for each query, put k-NN top-10 side by side with BM25 `multi_match` (martName/feature/keyword) top-10, and always also run the category-contamination demo (category filter vs vector). Success threshold: ≥ 3 queries in the non_overlap category where "vector hit@10 ≥ 1 and BM25 hit@10 = 0" (N=3 is the default and may be adjusted with the user at the review gate).

#### Scenario: review gate programmatically enforced
- **WHEN** `verify_search_os.py` is run against a golden set with `meta.status: draft`
- **THEN** the script immediately exits 1 and prompts that user review is needed, issuing no Bedrock or OpenSearch query

#### Scenario: quantifying the value of semantic search
- **WHEN** the full verification is run against the approved golden set
- **THEN** it produces `out/search_eval_{YYYYMMDD}.md` whose Summary includes a vector-only wins count; a count ≥ N (default 3) meets the success threshold; if not met it SHALL be reported honestly as a negative result and SHALL NOT loosen the criteria to pad the count

#### Scenario: control-group soundness
- **WHEN** reviewing the lexical_overlap category results
- **THEN** BM25 hit@10 performs normally (both sides find them), proving the control group was not weakened by segmentation or query design

#### Scenario: category-contamination demo
- **WHEN** a "health food" category query is run with a `categoryLevel1Name` filter side by side with a pure vector search
- **THEN** the report shows the filter missing brand-store products (e.g. a Ganoderma King under category="Grape King") while the vector finds it — quantified support for the POC business claim

#### Scenario: golden set is reusable
- **WHEN** Phase 2 runs the model benchmark (Titan vs Cohere)
- **THEN** the same approved YAML can be used directly as the recall@10 test set (the file is under git version control)

### Requirement: Safety contract — Bedrock notification, credential handling, security off for POC only

Before running `embed_products_os.py` (which hits real Bedrock, ~3.9M tokens ≈ <$0.1), the user SHALL be explicitly notified of the estimated cost and consent SHALL be obtained (tasks 5.1 gate); the P1-5 query embeddings are folded into the same authorization. Expired lab temporary credentials SHALL be refreshed with `scripts/refresh-lab-creds.sh` to resume, the `.env.local` key SHALL NOT be edited by hand, and no log / report / commit SHALL print an AWS access key. `DISABLE_SECURITY_PLUGIN=true` SHALL be used only for the local POC docker, and the compose file SHALL annotate this restriction. The 36MB source file SHALL be excluded by `.gitignore` and SHALL NOT enter git history. This change SHALL NOT touch the PostgreSQL schema (zero alembic migrations).

#### Scenario: spend gate
- **WHEN** the agent is about to run the embed script for the first time
- **THEN** the conversation contains a "cost estimate notification + explicit user consent" record; otherwise it must not run

#### Scenario: credentials expire
- **WHEN** boto3 raises ExpiredTokenException during embedding
- **THEN** the script prints the `./scripts/refresh-lab-creds.sh` guidance and exits; after refreshing, rerunning resumes from where it left off, and embeddings already paid for are not paid for again

#### Scenario: source file not committed
- **WHEN** `git status` is run after the source file is in place
- **THEN** `products/OpenSearch_Full_*.json` does not appear in the untracked list (`git check-ignore` matches)

#### Scenario: zero DB impact
- **WHEN** comparing `alembic current` and `alembic/versions/` before and after implementation
- **THEN** the revision is the same and there is no new migration file

### Requirement: Testing contract — test pure functions only, no network, no docker

`tests/test_product_search_units.py` SHALL exist and cover: JSON structure probing (both formats + raise on unknown format), `build_embed_text` (None→"", strip HTML, truncate boundary), and the golden set loader and approved-gate logic. The tests SHALL NOT require docker, network, or AWS credentials (aligned with the `tests/test_etl_units.py` conventions). OpenSearch / Bedrock I/O SHALL NOT have automated tests (decided; I/O verification goes through the manual curl/_count criteria in tasks). The three scripts SHALL isolate IO with `if __name__ == "__main__":` so tests can safely import the pure functions.

#### Scenario: tests independent of infrastructure
- **WHEN** `uv run pytest tests/test_product_search_units.py` is run in an environment with the OpenSearch container stopped and no AWS credentials
- **THEN** all pass, with zero network calls during the process

#### Scenario: importing scripts does not trigger IO
- **WHEN** tests load `load_products_os.py` / `embed_products_os.py` / `verify_search_os.py` via importlib
- **THEN** module loading itself does not connect to OpenSearch, does not build a boto3 client, and does not read the large source file

#### Scenario: existing tests unaffected
- **WHEN** `uv run pytest tests/test_etl_units.py` is run
- **THEN** it is still all green (this change does not touch any file under `src/recommender/`)
