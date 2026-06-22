# product-search-hybrid-api — Tasks

> 排序原則：由下而上（config/依賴 → 純函式核心 → I/O → service → router → app 整合 → 測試 → 評估 → verification），每個 phase 完成後 app 都應可啟動（`uvicorn recommender.main:app`）。
>
> **基礎設施需求標記**：
> - 🟢 無 docker / 無網路（可在 CI 跑）
> - 🟠 需 OpenSearch（`docker compose -f docker-compose.dev.yml --env-file .env.local up -d opensearch`，Phase 1 volume 已含 26,014 筆 + 向量）
> - 🔴 需真 Bedrock（**花錢，執行前必須告知使用者並取得同意** — safety.md §1）
>
> ⚠️ 本次**不應產生任何新 alembic migration**（search 用 OpenSearch 非 Postgres）。任何 task 做到一半發現「好像需要 migration」，停下來與使用者確認 —— 那代表偏離計劃範圍。

## Phase 1 — config 與依賴 🟢

- [x] **1.1** `pyproject.toml`：`"opensearch-py>=3.2.0"` 改 `"opensearch-py[async]>=3.2.0"`，`uv lock` 更新（本次唯一允許的依賴變動；aiohttp 應已是 aioboto3 傳遞依賴，此處改顯式宣告）。
      ✅ 判準：`uv run python -c "from opensearchpy import AsyncOpenSearch; print('ok')"` 輸出 `ok`。
- [x] **1.2** `src/recommender/config.py`：`Settings` 新增 OpenSearch 段（`opensearch_host="http://localhost:9200"`、`opensearch_index="products_v1"`）與 Bedrock Embedding 段（`bedrock_embed_model_id="amazon.titan-embed-text-v2:0"`、`bedrock_embed_region="ap-northeast-1"`、`embed_dimensions=1024`），對齊 design §2。
      ✅ 判準：`uv run python -c "from recommender.config import settings; print(settings.opensearch_index, settings.bedrock_embed_region)"` 輸出 `products_v1 ap-northeast-1`；設 `OPENSEARCH_INDEX=test_v2` 後重跑輸出 `test_v2`（env 綁定生效，不被 `extra="ignore"` 吞掉）。

## Phase 2 — search 模組純函式核心 🟢

- [x] **2.1** 建 `src/recommender/search/__init__.py` 與 `search/schemas.py`：`SearchResultItem`（`mart_id: str`、`mart_name: str`、`score: float`、`brand: str | None`、`price: float | None`、`category: str | None`）、`SearchResponse`（`query: str`、`results: list[SearchResultItem]`）。
      ✅ 判準：`uv run python -c "from recommender.search.schemas import SearchResponse; print(SearchResponse(query='x', results=[]).model_dump())"` 成功。
- [x] **2.2** `search/rrf.py`：`reciprocal_rank_fusion(result_lists: Sequence[Sequence[str]], k: int = 60) -> list[tuple[str, float]]`，公式 `score(doc)=Σ 1/(k+rank)`（rank 從 1 起算），score 降序、同分以 doc_id 字典序 tie-break；空清單 / 單邊缺漏合法。純函式、零 OpenSearch import（design §7）。
      ✅ 判準：`grep -n "opensearch\|pydantic" src/recommender/search/rrf.py` 為 0 筆；REPL 手算驗證 `reciprocal_rank_fusion([["a","b"],["b","c"]])` 中 `b` 的 score == 1/61 + 1/62 且排第一。
- [x] **2.3** `search/embeddings.py`：`@lru_cache get_bedrock_embeddings(model_id, region, profile, dimensions)` 回 langchain-aws `BedrockEmbeddings`（`model_kwargs={"dimensions": ..., "normalize": True}`，normalize hardcode —— design §3/§4）；`MOCK_QUERY_VECTOR = [1.0] + [0.0]*1023` 常數。
      ✅ 判準：`grep -n "lru_cache" src/recommender/search/embeddings.py` 命中；`uv run python -c "from recommender.search.embeddings import MOCK_QUERY_VECTOR; assert len(MOCK_QUERY_VECTOR)==1024 and sum(v*v for v in MOCK_QUERY_VECTOR)==1.0"` 通過（不建真 client、零網路）。
- [x] **2.4** `search/repository.py` 純函式部分：`build_knn_body(vector, k)` / `build_bm25_body(query_text, k)`，DSL 對齊 `scripts/etl/verify_search_os.py` 的 `knn_search` / `bm25_search`（knn 打 `embedding` 欄；BM25 `multi_match` 打 `martName`/`feature`/`keyword`）。
      ✅ 判準：兩函式為 module-level 純函式（非 method），可不建 client 直接 import 斷言 dict 結構。

## Phase 3 — repository I/O / service / router 🟢（撰寫不需 OpenSearch；行為驗證在 Phase 6）

- [x] **3.1** `search/client.py`：`@lru_cache(maxsize=1) get_opensearch_client() -> AsyncOpenSearch`（`hosts=[settings.opensearch_host]`，本地 security off 無認證）+ `async def close_opensearch_client()`（cache 有值才 `await client.close()`，之後 `cache_clear()`）—— design §9.1。
      ✅ 判準：`uv run python -c "from recommender.search.client import get_opensearch_client; c=get_opensearch_client(); assert get_opensearch_client() is c"`（單例；建構 lazy connect 不需 OpenSearch 在線）。
- [x] **3.2** `search/repository.py` I/O 部分：`SearchRepository(os_client, index)`，`async def hybrid_msearch(vector, query_text, k) -> tuple[list[dict], list[dict]]` —— msearch body 為 `[{"index": idx}, knn_body, {"index": idx}, bm25_body]` 交錯清單，回 `responses[0]`/`responses[1]` 的 hits；任一 response 含 `error` → raise（fail fast，design §6.2）。repository 不讀 settings。
      ✅ 判準：`grep -n "from recommender.config import settings" src/recommender/search/repository.py` 為 0 筆；`grep -n "msearch" src/recommender/search/repository.py` 命中。
- [x] **3.3** `search/service.py`：`SearchService(repo)` —— `__init__` 讀 `settings.analyzer_mock_mode`（對齊 AgentService 模式）；`async def search(query, size=10) -> SearchResponse`：`_embed_query`（mock → `MOCK_QUERY_VECTOR`；真 → `get_bedrock_embeddings(...).aembed_query(query)`）→ `hybrid_msearch(vector, query, k=2*size)` → `reciprocal_rank_fusion` → top-size → `_id→_source` map join 成 `SearchResultItem`（score=RRF 分）→ `SearchResponse`。查無結果回空 list 不拋例外。
      ✅ 判準：`grep -n "aembed_query" src/recommender/search/service.py` 命中（不用同步 `embed_query` 阻塞 event loop）；`grep -n "HTTPException\|raise NotFoundError" src/recommender/search/service.py` 為 0 筆；回傳型別註記為 `SearchResponse` 非 dict/raw hits。
- [x] **3.4** `search/router.py`：`APIRouter(prefix="/search", tags=["search"])`，`GET ""`：`q: str = Query(min_length=1)`、`size: int = Query(default=10, ge=1, le=100)`，注入 `SearchServiceDep`，`response_model=SearchResponse`。不 try/except、不拋 HTTPException。
      ✅ 判準：`grep -n "HTTPException\|SearchRepository\|AsyncOpenSearch" src/recommender/search/router.py` 為 0 筆（router 不碰 repo/client）。

## Phase 4 — app 整合（deps / lifespan / router 掛載）🟢

- [x] **4.1** `src/recommender/deps.py`：新增 `get_os_client`（回 `get_opensearch_client()` 單例）+ `OSClientDep`、`get_search_repository(os_client)`（傳入 `settings.opensearch_index`）+ `SearchRepoDep`、`get_search_service(repo)` + `SearchServiceDep` —— deps.py 維持唯一 wiring 點（design §9.3）。
      ✅ 判準：`grep -rn "Depends(" src/recommender/search/` 僅 router.py 的 `SearchServiceDep` 使用處（wiring 定義全在 deps.py）；app import 無 DI 錯誤。
- [x] **4.2** `src/recommender/main.py`：(a) lifespan startup 加 `get_opensearch_client()`（建物件，lazy connect）與 `_preheat_embeddings()`（mock 時跳過、失敗只 log warning，比照 `_preheat_llm`）；(b) shutdown 加 `await close_opensearch_client()`（更新「Shutdown 不用做事」註解）；(c) `app.include_router(search.router)`。
      ✅ 判準：`ANALYZER_MOCK_MODE=true uv run uvicorn recommender.main:app` 在 **OpenSearch 未啟動**時仍可正常啟動與關閉（client 建構 lazy、預熱 mock 跳過、shutdown close 不噴未處理例外）；OpenAPI `/docs` 出現 `GET /search`。
- [x] **4.3** 迴歸確認：既有測試不被整合破壞。
      ✅ 判準：`uv run pytest tests/test_etl_units.py tests/test_chains.py tests/test_product_search_units.py` 全綠（🟢 不需 docker）。

## Phase 5 — 單元測試 🟢

- [x] **5.1** `tests/test_search_units.py` — RRF：雙清單融合手算驗證（`b` 在兩清單 → 1/61+1/62 排第一）、`k` 參數變化影響排序分數、空輸入（`[]` 與 `[[], []]`）回空、單邊缺漏（一清單空）等同單路排名、同分 tie-break deterministic（同輸入跑兩次結果相同）。
      ✅ 判準：`uv run pytest tests/test_search_units.py -k rrf` 全綠，零網路零 docker。
- [x] **5.2** 同檔 — DSL builder：`build_knn_body` 含 `query.knn.embedding.vector`（原樣帶入）與 `size==k`；`build_bm25_body` 含 `multi_match.fields == ["martName","feature","keyword"]`。
      ✅ 判準：`uv run pytest tests/test_search_units.py -k body` 全綠。
- [x] **5.3** 同檔 — mock 向量不變量：`MOCK_QUERY_VECTOR` 長度 1024、L2 norm == 1.0。
      ✅ 判準：對應測試全綠。
- [x] **5.4** 同檔 — `SearchService` 編排（fake repo 注入，不碰 OpenSearch/Bedrock）：mock mode 下注入回預製 knn/bm25 hits 的 fake repo，斷言融合排序、top-size 截斷、`SearchResultItem` 欄位映射（`_id`→`mart_id`、`_source.martName`→`mart_name`、score=RRF 分）、兩邊皆空 → `results==[]` 不拋例外。
      ✅ 判準：`uv run pytest tests/test_search_units.py` 全檔全綠，conftest 的 `ANALYZER_MOCK_MODE=true` 生效、零 Bedrock 呼叫。

## Phase 6 — mock-mode API smoke 🟠（需 OpenSearch，不需 Bedrock）

- [x] **6.1** `tests/test_search_api_smoke.py`：collection-time ping `localhost:9200`，不可達 → 整模組 skipif（對齊 `test_pipeline_e2e.py` 的 reachability 模式，docstring 寫明前置指令）。
      ✅ 判準：OpenSearch 停止時 `uv run pytest tests/test_search_api_smoke.py` 顯示 skipped 而非 failed。
- [x] **6.2** 同檔 smoke 斷言（`httpx.AsyncClient` + `ASGITransport`，mock mode）：
      (a) `GET /search?q=掃地機器人` → 200、`results` 非空、每筆含 `mart_id`/`mart_name`/`score`、score 降序；
      (b) **融合證據**：結果包含 BM25 詞面命中商品（mock 向量下 k-NN 是 deterministic 噪音、BM25 是真的 —— 詞面強 query 的結果必含 BM25 貢獻）；
      (c) 邊界：`size=101` → 422、`q=`（空字串）→ 422、`size=1` → 恰 1 筆；
      (d) 查無結果 query（如 `q=zzzzqqqq不存在詞`）→ 200 + `results==[]`（**非 404**）。
      ✅ 判準：起 OpenSearch 後 `uv run pytest tests/test_search_api_smoke.py` 全綠；測試期間零 Bedrock 呼叫（`ANALYZER_MOCK_MODE=true`）。
- [x] **6.3** 手動 smoke（uvicorn 起 app，mock mode）：`curl -s "localhost:8000/search?q=靈芝保健飲&size=5" | jq .`，肉眼確認 JSON 結構與 5 筆結果。
      ✅ 判準：curl 回 200 + 合法 `SearchResponse` JSON。

## Phase 7 — 準確度評估 🔴（opt-in；真 Bedrock + OpenSearch；**執行前花錢告知 gate**）

- [x] **7.1** 新增 `scripts/etl/judge_hybrid_search.py`：重用 `load_golden_set`（approved gate 照舊 exit 1 強制）與 `judge_search_relevance.py` 的 LLM-judge 方法（Opus 級 judge，`JUDGE_MODEL_ID` env 可覆寫）；對運行中 app（mock OFF）打 `GET /search` 取 hybrid top-10，**同一輪**並排 k-NN-only 與 BM25-only（同 judge 同批，避免跨輪漂移），輸出 `out/search_eval_hybrid_{YYYYMMDD}.md` 三欄比較（design §10.3）。
      ✅ 判準：腳本存在、module docstring 寫明輸入/輸出/成本估算/safety 告知要求；`meta.status != approved` 時 exit 1 不發任何外部呼叫。
- [x] **7.2** 執行評估（**gate：先向使用者告知預估成本（量級 < $1）並取得同意**；lab 憑證過期跑 `scripts/refresh-lab-creds.sh`）。
      ✅ 判準：對話中存在成本告知 + 同意紀錄；報告產出。
- [x] **7.3** 判定成功標準（如實回報，不調寬）：
      (a) 全局：hybrid 相關數 ≥ max(vec-only 相關數, bm25-only 相關數)；
      (b) 互補保留：向量強項 query（情境式，如 q11/q13）與 BM25 強項 query（q04 ThinkPad）hybrid 相關數均不歸零。
      ✅ 判準：報告 Summary 明列兩項判定與數據；未達標如實標 ❌ 並回報使用者，不修改標準湊數。

## Phase 8 — Verification（全項完成後）

- [x] **8.1** 全測試綠：`uv run pytest`（前置：postgres + opensearch 容器在線；不在線時對應模組 skip 也算通過，但本機驗收應兩者皆起）。
      ✅ 判準：0 failed。
- [x] **8.2** **零 migration 驗證**：開工前記下 `alembic current` 輸出，完工後再跑一次 revision 相同；`git status alembic/versions/` 無新檔。
      ✅ 判準：revision 前後一致、`alembic/versions/` 無 untracked 檔。
- [x] **8.3** 邊界 grep 全套：
      - `grep -rn "HTTPException" src/recommender/search/` → 0 筆
      - `grep -rn "from recommender.config import settings" src/recommender/search/repository.py` → 0 筆（repo 不讀 settings）
      - `grep -rn "SearchRepository\|AsyncOpenSearch" src/recommender/search/router.py` → 0 筆（router 不越層）
      - `grep -rn "def get_search" src/recommender/` 僅命中 `deps.py`（wiring 唯一點）
      - `grep -rn "normalize" src/recommender/search/embeddings.py` 命中且為 hardcode `True`（非讀 settings）
- [x] **8.4** lifespan 生命週期驗證：起 uvicorn（mock mode）後 Ctrl-C，log 無 aiohttp `Unclosed client session` warning（client 有被 `await close()`）。
      ✅ 判準：shutdown log 乾淨。
- [x] **8.5** 文件同步：`docs/plans/product-search-vectorization.md` §Phase 2 標註「已規格化為 openspec/changes/product-search-hybrid-api」；`docs/architecture/architecture.md` 補 `search/` 領域模組一節（含「層優先 codebase 的刻意例外」設計決策記載）。
      ✅ 判準：兩處 grep 得到 `product-search-hybrid-api` 字樣。
