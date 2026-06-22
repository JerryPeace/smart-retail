# product-search-vectorization — Tasks

> Ordering principle: dependency chain — prerequisites → P1-1 docker → P1-2 index → P1-3 load → pure-function tests (before any Bedrock spend) → P1-4 embed (Bedrock spend gate) → golden set drafting → **user review gate** → P1-5 verification → verification.
>
> Tasks marked **【USER】** are user actions; the agent must not do them and may only prompt for them. Two hard gates: **5.1 (Bedrock spend consent)** and **6.2 (golden set review)** — until a gate is passed, the tasks that follow it must not start.
>
> ⚠️ This change involves zero DB migrations and zero changes to `src/recommender/`. If midway you feel you need either → stop and confirm with the user (it signals scope drift).

## Phase 0 — Prerequisites

- [x] **0.1【USER】** Place the source file at `products/OpenSearch_Full_20260612_030007.json` (the directory is currently empty; **runtime blocker**: while 0.1 is incomplete, everything from Phase 3 onward is blocked, but Phase 1 (docker) can proceed first).
      ✅ Criterion: `ls -lh products/OpenSearch_Full_20260612_030007.json` shows about 36MB.
- [x] **0.2** Add `products/OpenSearch_Full_*.json` to `.gitignore` (36MB is not committed; use a wildcard to cover future monthly dumps).
      ✅ Criterion: `git check-ignore products/OpenSearch_Full_20260612_030007.json` exits 0; `git status` does not show the file.
- [x] **0.3** `uv add opensearch-py` (pyproject.toml currently lacks this dependency).
      ✅ Criterion: `uv run python -c "import opensearchpy; print(opensearchpy.__version__)"` prints normally; `git diff pyproject.toml` adds only this one dependency.

## Phase 1 — P1-1 Local OpenSearch (docker)

- [x] **1.1** Create `docker/opensearch/Dockerfile`: `FROM opensearchproject/opensearch:<latest patch tag in the 2.19 series, look up on Docker Hub and pin>` + `RUN bin/opensearch-plugin install --batch analysis-smartcn`.
      ✅ Criterion: `docker build docker/opensearch/` succeeds.
- [x] **1.2** Add an `opensearch` service to `docker-compose.dev.yml`: `build:` pointing to 1.1, `container_name: marketing-poc-opensearch`, single-node, `DISABLE_SECURITY_PLUGIN=true`, `DISABLE_INSTALL_DEMO_CONFIG=true`, `OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g`, memlock ulimits, `9200:9200`, `opensearch_data` volume, `app-network`, healthcheck (`curl -sf http://localhost:9200/_cluster/health`, interval 10s / retries 12, aligned with existing conventions). Annotate the security-off setting with "local POC only".
      ✅ Criterion: after `docker compose -f docker-compose.dev.yml up -d opensearch`, `docker inspect --format '{{.State.Health.Status}}' marketing-poc-opensearch` is `healthy`; `curl -s localhost:9200/_cluster/health | jq -r .status` is `green`.
- [x] **1.3** Verify the smartcn plugin is active.
      ✅ Criterion: `curl -s localhost:9200/_cat/plugins` includes `analysis-smartcn`; `curl -s -XPOST localhost:9200/_analyze -H 'Content-Type: application/json' -d '{"analyzer":"smartcn","text":"靈芝保健飲品"}' | jq '[.tokens[].token]'` segments into words (e.g. "靈芝", "保健"), not character-by-character.
- [x] **1.4** (Optional) Add an `opensearch-dashboards` service (same tag, `5601:5601`, `DISABLE_SECURITY_DASHBOARDS_PLUGIN=true`, `profiles: [dashboards]`).
      ✅ Criterion: after `docker compose --profile dashboards up -d`, `curl -sf localhost:5601/api/status` returns 200; the default (no profile) `docker compose up -d` does not start it.

## Phase 2 — P1-2 Build the k-NN index

- [x] **2.1** Define `INDEX_SETTINGS` / `INDEX_MAPPING` constants at the top of `scripts/etl/load_products_os.py` (full table in design §2: `index.knn=true`, replicas 0, `refresh_interval=-1` during loading; smartcn text fields ×3, keyword fields, price float, isSearchable integer, `embedding` knn_vector 1024/faiss/hnsw/innerproduct); the script calls `indices.create` on startup (skip if it already exists).
      ✅ Criterion (after creation): `curl -s localhost:9200/products_v1/_mapping | jq '.products_v1.mappings.properties.embedding'` shows `dimension: 1024`, `engine: "faiss"`, `space_type: "innerproduct"`, `name: "hnsw"`; `curl -s localhost:9200/products_v1/_settings | jq '.products_v1.settings.index.knn'` is `"true"`; the analyzer for `martName` is `smartcn`.

## Phase 3 — P1-3 Load raw data (depends on 0.1, Phases 1–2)

- [x] **3.1** Complete `scripts/etl/load_products_os.py` (design §3): pure functions `detect_format` / `extract_sources` (handle both the plain-array and search-response-hits structures, fail fast on unknown formats) → filter `isSearchable == 1` → `helpers.bulk` (`index` action, `_id=str(martId)`, chunk 500) → restore `refresh_interval="1s"` → print a summary (total / filtered / error counts; exit 1 if bulk errors > 0). Match the style of `scripts/etl/aggregate_monthly.py` (docstring, top-of-file constants, main guard).
      ✅ Criterion: `uv run python scripts/etl/load_products_os.py` finishes successfully; `curl -s localhost:9200/products_v1/_count | jq .count` ≈ 26014 (26018 − 4 records with isSearchable=0; defer to the actual filter log).
- [x] **3.2** Idempotency check: run the same command from 3.1 again.
      ✅ Criterion: after the second run, `_count` is **exactly the same** as the first (`index` action + `_id=martId` overwrite does not double); `refresh_interval` is `"1s"`.

## Phase 4 — Write the embed script + pure-function tests (before any Bedrock spend)

- [x] **4.1** Write `scripts/etl/embed_products_os.py` (design §4, **this task only writes, does not run**): must_not exists `embedding` to fetch missing docs → `build_embed_text` pure function (martName+feature+keyword+three-level category; strip HTML, `or ""` to guard against literal None, whitespace collapse, 50k truncate) → boto3 `Session(profile_name="lab", region_name="ap-northeast-1")` per-thread, body `{"inputText", "dimensions": 1024, "normalize": true}` → exponential backoff (429/5xx/Throttling, max 8 attempts) → `update` action bulk write-back (batch 200–500) → ThreadPoolExecutor default 8 workers → on `ExpiredTokenException`, print the `./scripts/refresh-lab-creds.sh` resume guidance.
      ✅ Criterion: `uv run python -c "import importlib.util as u; s=u.spec_from_file_location('m','scripts/etl/embed_products_os.py'); m=u.module_from_spec(s); s.loader.exec_module(m)"` imports successfully with **zero network calls** (the main guard isolates IO).
- [x] **4.2** Write `tests/test_product_search_units.py` (design §6, aligned with the `tests/test_etl_units.py` conventions, no DB / no network / no docker): `detect_format`/`extract_sources` for both structures + raise on unknown format; `build_embed_text`'s None→"" (assert the output contains no literal `"None"`), strip HTML, truncate boundary; the golden set loader's schema and its `status != approved` refusal logic (the loader function may exist in the same file or in a temporary location before the verify script is complete, with its final home in `verify_search_os.py`). Load the script modules with `importlib.util.spec_from_file_location`.
      ✅ Criterion: `uv run pytest tests/test_product_search_units.py -v` is all green, **with no docker needed** (passes even while `docker compose stop opensearch`); the existing `uv run pytest tests/test_etl_units.py` is still all green.

## Phase 5 — P1-4 Run vectorization (Bedrock notification gate)

- [x] **5.1【USER】＝ GATE** May run only after notifying the user and obtaining consent: this step hits **real AWS Bedrock** (Titan v2, profile `lab`, ap-northeast-1); 26,014 records ≈ 3.9M tokens ≈ **< $0.1 one-time**; the ~20 query embeddings in P1-5 (negligible cost) are authorized together. Estimated wall-clock 10–20 min (8 concurrent).
      ✅ Criterion: there is an explicit record of user consent in the conversation. **5.2 must not run before consent.**
- [x] **5.2** After confirming the lab credentials are valid (run `./scripts/refresh-lab-creds.sh` first if needed), run `uv run python scripts/etl/embed_products_os.py`. Credentials expiring midway is expected: after refreshing, rerun the same command to resume.
      ✅ Criterion: `curl -s -XPOST localhost:9200/products_v1/_count -H 'Content-Type: application/json' -d '{"query":{"exists":{"field":"embedding"}}}' | jq .count` == the total `products_v1/_count` (missing embeddings = 0).
- [x] **5.3** Resume-mechanism check (if 5.2 finished in one pass, verify by "Ctrl-C once midway, then rerun"): after interruption, rerun; the log shows it processes only the remaining docs missing embeddings and does not re-embed completed ones.
      ✅ Criterion: the "embedded this round" count in the rerun log < total, final missing = 0; spot-check a random record with `curl -s localhost:9200/products_v1/_doc/<martId> | jq '.._source.embedding | length'` is 1024.

## Phase 6 — golden set (agent drafts → user review gate)

- [x] **6.1** The agent drafts `scripts/etl/golden_set_product_search.yaml` from the source JSON (design §5.1): 15–20 entries, two categories `lexical_overlap` + `non_overlap` with **non_overlap ≥ 8 entries**, each entry containing `query`/`category`/`expected_mart_ids`/`rationale`, and `meta.status: draft`. Verify each `expected_mart_ids` exists in the source file one by one via jq/grep (do not fabricate); include ≥2 entries that hit category-contaminated products (e.g. Grape King Ganoderma King). non_overlap entries must be grep-verified that the query keywords do **not** appear in the expected products' martName/feature/keyword (otherwise classify as lexical_overlap).
      ✅ Criterion: the YAML parses under the 4.2 loader test; `yq '.queries | length'` is 15–20; `yq '[.queries[] | select(.category=="non_overlap")] | length'` ≥ 8; the verification command and result for each entry's expected_mart_ids are attached to the delivery message.
- [x] **6.2【USER】＝ GATE** Review the golden set: go through each entry — whether the query reads like a real query and whether expected_mart_ids is reasonable — and approve after any additions/deletions/edits; at the same time confirm whether the success threshold N=3 (design §5.4) is accepted or adjusted. After approval, change `meta.status` to `approved` and fill in `approved_by`/`approved_at`.
      ✅ Criterion: `yq '.meta.status' scripts/etl/golden_set_product_search.yaml` is `approved`. **Phase 7 must not run before approval (the verify script enforces exit 1 programmatically).**

## Phase 7 — P1-5 Verify search results

- [x] **7.1** Complete `scripts/etl/verify_search_os.py` (design §5.3–5.4): at the start check `meta.status == approved` or exit 1 → embed each query with Titan v2 (`normalize:true`, same model as the docs) → put k-NN top-10 vs BM25 `multi_match` (martName/feature/keyword) top-10 side by side → compute hit@10 against expected_mart_ids → always also run the category-contamination demo (category filter misses Grape King vs vector finds it) → output `out/search_eval_{YYYYMMDD}.md` (side-by-side table + Summary).
      ✅ Criterion: running against a `status: draft` YAML exits 1 and prints the hint (programmatic gate verification); after approval, `uv run python scripts/etl/verify_search_os.py` produces the report file.
- [x] **7.2** Run the verification and interpret it against the success threshold.
      ✅ Criterion: `out/search_eval_*.md` exists and contains (a) the vector/BM25 side-by-side top-10 and hit@10 for each query, (b) a vector-only wins count in the Summary **≥ 3** (or the adjusted N from 6.2), (c) a normal BM25 hit rate for the `lexical_overlap` category (the control group is not a straw man), (d) the category-contamination demo section (filter misses / vector finds). If < N: report honestly, discuss with the user (adjust the golden set or record it as a POC negative result); **do not** loosen the criteria to pad the count.

## Phase 8 — Verification (final check)

- [x] **8.1** Full test suite and idempotency final check: `uv run pytest` all green (including existing tests; prerequisite for e2e is `docker compose up -d postgres`); after rerunning `load_products_os.py`, `_count` is unchanged and embeddings are not lost (after the `index` overwrite, the missing-embedding count should be 0, or be backfilled by an embed resume run — record the measured result in the report).
      ✅ Criterion: pytest exit 0; record the four numbers — `_count` and exists-embedding count before and after the load rerun — in the delivery message.
- [x] **8.2** Scope and safety final check:
      ✅ Criterion: `git status` has no 36MB source file; `alembic current` is the same as before work started, and `alembic/versions/` has no new files; `git diff --stat` contains only docker/opensearch/, docker-compose.dev.yml, .gitignore, pyproject.toml/uv.lock, the three new scripts under scripts/etl/ + the golden set, and one new test file under tests/; logs and reports contain no AWS key.
- [x] **8.3** Deliver a summary to the user: number of records loaded, embedding coverage, the order of magnitude of actual Bedrock spend, the golden set verification conclusion (whether the success threshold was met), the `out/search_eval_*.md` path, and Phase 2 recommendations (the handoff points for hybrid/RRF and model benchmarking).
      ✅ Criterion: the summary is sent and cites concrete numbers (not a one-line "done").
