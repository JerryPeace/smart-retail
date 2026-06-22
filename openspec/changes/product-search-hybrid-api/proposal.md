# product-search-hybrid-api

> 上游計劃：`docs/plans/product-search-vectorization.md` §Phase 2（hybrid + search API，已獲 Phase 1 實證支持）。
> 前置變更：`openspec/changes/product-search-vectorization/`（Phase 1，✅ 已收案 2026-06-13）—— 26,014 筆商品已載入本地 docker OpenSearch `products_v1` 並 100% Titan v2 向量化（1024 維、normalize:true）。
> 本變更把 Phase 1 的 POC scripts 升級為**建進 app 的 hybrid search API**。

## Why

Phase 1 收案數據（三輪量尺、Opus 4.8 judge 採信）給出明確結論：**單一搜尋方法都不夠，hybrid 是正解**：

1. **向量強項實證 —— 情境/症狀式查詢**：「冬天戶外手腳冰冷」vec 4:0、「頭髮掉太多想變茂密」vec 7:2。零詞面重疊的身體狀態描述，BM25 全滅、向量有效。
2. **BM25 強項實證 —— 品牌/型號式查詢**：「ThinkPad 筆電」vec 1:10 —— 嵌入被規格/類別文字稀釋，BM25 詞面精確命中。兩個 judge 在此完全一致（均 0:10 / 1:10），是向量的結構性短板。
3. **互補性量化 —— hybrid 的直接實證依據**：全局 vec_only_rel vs bm25_only_rel，Haiku judge **57 vs 73**、Opus judge **41 vs 52**，兩個 judge 方向一致 —— 兩方法各自找到對方漏掉的**數十筆**相關商品。只上任一方法，就是把另一邊的相關結果整批丟掉。

但 Phase 1 的產物是 `scripts/etl/` 下的一次性驗證腳本（同步 boto3、print 報告），不是可被下游消費的服務。要讓商品搜尋成為 app 能力（供未來推薦情境、HubSpot 等下游使用），需要：

- 一個走三層紀律的 **`GET /search` endpoint**，BM25 + k-NN 雙路查詢、RRF 融合、回結構化 DTO；
- query embedding 整合進 app 的 Bedrock 設定（含 **mock 路徑** —— 否則每次本機開發打 /search 都燒真 Bedrock，違反 safety.md）；
- 用 Phase 1 留下的 golden set + LLM-judge 量尺，驗證 **hybrid 不劣於單一方法**。

## What Changes

對齊計劃文件 §Phase 2 勾勒與本次三個已拍板決策（範圍=只做核心 hybrid search API；融合=應用端 Python RRF；query embedding=app Bedrock 設定 + mock 路徑）：

- **領域模組 `src/recommender/search/`**（self-contained bounded context，計劃 §Phase 2 已拍板的刻意例外，見 design §1）：
  - `schemas.py` — `SearchResultItem` / `SearchResponse` DTO 與查詢參數約束（`q` 必填、`size` 預設 10 上限 100）。
  - `repository.py` — `SearchRepository(os_client, index)`：**AsyncOpenSearch** + `msearch` 一次併發 k-NN 與 BM25 兩查詢；DSL body 建構抽成純函式（`build_knn_body` / `build_bm25_body`，自 `scripts/etl/verify_search_os.py` lift 改 async）。
  - `rrf.py` — `reciprocal_rank_fusion(...)` 純函式（`score(doc) = Σ 1/(k+rank)`，k 預設 60），好單測、零 I/O。
  - `service.py` — `SearchService(repo)`：embed query（mock mode 回固定向量）→ hybrid msearch → RRF 融合 → top-size → 映射 DTO。查無結果回空 list（**200 非 404**）。
  - `embeddings.py` — `@lru_cache get_bedrock_embeddings(...)` 回 langchain-aws `BedrockEmbeddings`（比照 `llm.py` 的 cached builder 模式）。
  - `router.py` — `GET /search`，注入 `SearchServiceDep`。
- **Settings 新增**（`config.py`）：`opensearch_host` / `opensearch_index` / `bedrock_embed_model_id` / `bedrock_embed_region`（Titan 在東京 lab，與 LLM 的 `bedrock_region=us-east-1` 不同）/ `embed_dimensions`。mock 沿用既有 `analyzer_mock_mode`，不加新 flag。
- **app 整合**：`main.py` lifespan startup 建 AsyncOpenSearch client + 非 mock 時預熱 embeddings client、shutdown **async close** client；`deps.py`（唯一 wiring 點）新增 `get_opensearch_client` / `get_search_repository` / `get_search_service` providers；`main.py` `include_router(search.router)`。
- **依賴變動（本次唯一允許）**：`opensearch-py` 改宣告 `opensearch-py[async]`（async 客戶端需 aiohttp，官方 async guide 指定的安裝方式；aiohttp 已是 aioboto3 的傳遞依賴，此處改為顯式宣告）。
- **測試三層**（對齊既有慣例）：
  1. **單元（CI、無 docker 無網路）** `tests/test_search_units.py`：RRF 純函式、DSL builder、mock 向量不變量。
  2. **mock-mode API smoke（需 OpenSearch、不需 Bedrock）**：`GET /search` 回 200 + 結構正確 + 融合兩邊；OpenSearch 不可達時整模組 skip（對齊 `test_pipeline_e2e.py` 模式）。
  3. **準確度評估（opt-in，真 Bedrock + OpenSearch，花錢 gate）**：重用 golden set（15 條 approved）+ LLM-judge 方法（`judge_search_relevance.py` 量尺、Opus 級 judge）對 `/search` hybrid 端點評相關性，同輪並排 hybrid / k-NN / BM25 三欄，驗證 hybrid 不劣於單一方法。
- **零 alembic migration**：search 全走 OpenSearch，不碰 PostgreSQL schema（verification 強制驗證項）。

## Out of Scope（本次明確不做）

| 項目 | 為什麼不做 |
|------|-----------|
| `category` / `stock` 軟訊號降權 | **Phase 2b**。先讓核心 hybrid 上線取得基線，再疊加排序訊號（計劃 §Phase 2 後段） |
| 中文 embedding 模型 benchmark（Titan vs Cohere Multilingual） | **Phase 2b**。golden set 已留作固定量尺，benchmark 不阻塞 API 建置 |
| OpenSearch native search pipeline（score-ranker-processor / normalization-processor） | 已拍板用**應用端 Python RRF**：可重用 POC 的 knn/bm25 查詢、RRF 是純函式好單測（取捨見 design §9） |
| 修分類污染（FM 重新分類 363 筆品牌館商品） | **Phase 3**（計劃 §Phase 3） |
| HubSpot 同步、推薦情境接 /search | 下游消費是後續變更；本次只交付可被消費的 API |
| 上 prod、prod OpenSearch 對齊 | POC 不綁 prod（計劃定位不變） |
| POST /search、分頁 cursor、filter 參數 | Simplicity First：核心契約是 `GET /search?q=&size=`，其餘等真實需求 |
| 改動 `scripts/etl/` 三支 Phase 1 腳本 | 查詢函式以「lift 進 search 模組改 async」方式重用，原腳本保留作 ETL/重建工具，不回頭重構 |
| 新增 alembic migration / 碰 PostgreSQL | 本次資料面只讀 OpenSearch（verification 驗證項） |
