# Spec: product-search-hybrid-api

本規格定義 Phase 2（hybrid search API）完成後必須成立的六份契約：搜尋契約、分層契約、embedding 契約、async 契約、測試契約、安全契約。實作完成後，任何違反下列 Requirement 的產物視為未通過驗收。對齊 `docs/plans/product-search-vectorization.md` §Phase 2 與本變更 design.md 的拍板決策（應用端 RRF、領域模組、mock 預設）。

## ADDED Requirements

### Requirement: 搜尋契約 — `GET /search` hybrid 融合、空結果 200、size 上限

`GET /search` SHALL 接受 `q`（必填，`min_length=1`）與 `size`（預設 10、範圍 1–100），對 `products_v1` **同時**執行 k-NN（query 向量）與 BM25（`multi_match` 打 `martName`/`feature`/`keyword`）兩路查詢（每路候選數 = 2×size），以**應用端 Python RRF**（`score(doc)=Σ 1/(k+rank)`，k=60）融合後回 top-size。回應 SHALL 為 `SearchResponse`（`query` + `results: list[SearchResultItem]`），`results` 依 RRF 融合分降序；`SearchResultItem.score` SHALL 為 RRF 融合分而非 OpenSearch `_score`（兩路 `_score` 量綱不可比）。查無結果 SHALL 回 **HTTP 200 + 空 `results`**，SHALL NOT 回 404 —— 「搜尋沒中」是正常業務結果。融合 SHALL NOT 使用 OpenSearch search pipeline / score-ranker-processor（已拍板應用端 RRF）。

#### Scenario: hybrid 融合生效
- **WHEN** 以 `GET /search?q=掃地機器人` 查詢（OpenSearch 已載入 Phase 1 的 26,014 筆 + 向量）
- **THEN** 回 200，`results` 非空、依 `score` 降序，且結果集合為 k-NN 與 BM25 兩路候選經 RRF 融合的聯集 top-size（兩路皆命中的文件分數疊加、排序靠前）

#### Scenario: 空結果非 404
- **WHEN** 查詢一個兩路皆無命中的字串（如隨機亂碼）
- **THEN** 回 HTTP 200 且 `results == []`，不拋例外、不回 404

#### Scenario: 參數邊界
- **WHEN** 請求 `size=101` 或 `size=0` 或 `q=`（空字串）
- **THEN** FastAPI 驗證回 422；`size=1` 時回恰 1 筆

#### Scenario: RRF 純函式正確性
- **WHEN** 對 `reciprocal_rank_fusion([["a","b"],["b","c"]], k=60)` 求值
- **THEN** `b` 的分數為 `1/61 + 1/62` 且排第一；同分文件以 doc_id 字典序 tie-break，同輸入重複執行結果完全相同（deterministic）

### Requirement: 分層契約 — 領域模組內三層紀律、wiring 集中 deps.py

`src/recommender/search/` SHALL 為自含領域模組（router / service / repository / schemas / rrf / embeddings / client），且模組內部 SHALL 遵守既有三層紀律：router SHALL 只做參數驗證與呼叫 service，SHALL NOT import `SearchRepository` / `AsyncOpenSearch`、SHALL NOT 拋 `HTTPException`（未預期錯誤交給 `main.py` 全域 handler）；service SHALL 回傳 Pydantic DTO（`SearchResponse`），SHALL NOT 把 raw OpenSearch hit dict 外洩給 router；repository SHALL 只做 DSL 建構與 msearch I/O，SHALL NOT 讀 `settings`（`os_client` 與 `index` 由 deps 注入）、SHALL NOT 含融合或 DTO 轉換邏輯。DSL body 建構（`build_knn_body` / `build_bm25_body`）與 RRF SHALL 為 module-level 純函式，可在無 client 下 import 測試。所有 DI providers（`get_os_client` / `get_search_repository` / `get_search_service`）SHALL 定義於 `deps.py`（唯一 wiring 點），SHALL NOT 在 search 模組內另建 wiring。

#### Scenario: router 不越層
- **WHEN** 檢查 `grep -rn "SearchRepository\|AsyncOpenSearch\|HTTPException" src/recommender/search/router.py`
- **THEN** 0 筆命中

#### Scenario: repository 不讀全域設定
- **WHEN** 檢查 `grep -n "from recommender.config import settings" src/recommender/search/repository.py`
- **THEN** 0 筆命中

#### Scenario: wiring 唯一點
- **WHEN** 檢查 `grep -rn "def get_search\|def get_os_client" src/recommender/`
- **THEN** 僅 `deps.py` 命中

### Requirement: embedding 契約 — query/doc 同模型同參數同維度、mock 路徑

query embedding SHALL 使用與 Phase 1 doc 端完全相同的模型與參數：`amazon.titan-embed-text-v2:0`、`dimensions=1024`、`normalize=true` —— 此為向量空間一致性不變量（innerproduct 等價 cosine 的前提），`normalize=true` SHALL hardcode 於 embeddings builder，SHALL NOT 做成可調 Settings。embeddings client SHALL 以 `@lru_cache` builder 建構（比照 `llm.py` 模式，process 層級共用），region 走 `bedrock_embed_region`（ap-northeast-1，與 LLM 的 us-east-1 分開設定）。`analyzer_mock_mode=true` 時 service SHALL 回固定 1024 維**單位向量**（`MOCK_QUERY_VECTOR`），SHALL NOT 發出任何 Bedrock 呼叫；mock 下 BM25 路徑照常執行。更換 embedding 模型 SHALL 伴隨全量重嵌 + 新索引（reindex/alias），SHALL NOT 只改 Settings 了事。

#### Scenario: mock mode 零 Bedrock
- **WHEN** `ANALYZER_MOCK_MODE=true` 下呼叫 `GET /search?q=任意查詢`
- **THEN** 端到端回 200（k-NN 以 `MOCK_QUERY_VECTOR` 合法執行、BM25 真實執行、RRF 正常融合），全程零 Bedrock 呼叫、不需 AWS 憑證

#### Scenario: mock 向量不變量
- **WHEN** 檢驗 `MOCK_QUERY_VECTOR`
- **THEN** 長度 == 1024 且 L2 norm == 1.0（保 innerproduct 語意合法）

#### Scenario: 真 embedding 參數一致
- **WHEN** mock OFF 下發出 query embedding 請求
- **THEN** 模型為 `amazon.titan-embed-text-v2:0`、request 含 `dimensions=1024` 與 `normalize=true`，回傳向量長度 1024 —— 與 `scripts/etl/embed_products_os.py` doc 端嵌入完全同參數

### Requirement: async 契約 — 不阻塞 event loop、client 生命週期受 lifespan 管理

search 全鏈路 SHALL 為 async：OpenSearch 存取 SHALL 用 `AsyncOpenSearch`（`opensearch-py[async]`，aiohttp connection），SHALL NOT 在 async 路徑使用同步 `OpenSearch` client；兩路查詢 SHALL 以單次 `msearch`（一次 round-trip、server 端並行）發出，msearch 任一 per-response error SHALL fail fast 上拋（全域 handler 轉 500），SHALL NOT 靜默降級為單路。真 embedding 呼叫 SHALL 走 `aembed_query`（executor 包裹），SHALL NOT 在 event loop 直接呼叫同步 `embed_query`。AsyncOpenSearch client SHALL 為 process 單例（`@lru_cache` builder），於 lifespan startup 建構（lazy connect，OpenSearch 離線不擋 app 啟動）、shutdown SHALL `await client.close()` 釋放 aiohttp session。

#### Scenario: app 啟動不依賴 OpenSearch 在線
- **WHEN** OpenSearch 容器未啟動時以 mock mode 起 uvicorn
- **THEN** app 正常啟動（client 建構不連線）；此時打 `/search` 回 500（連線失敗走全域 handler），app 不崩潰

#### Scenario: shutdown 乾淨關閉
- **WHEN** uvicorn 收到關閉訊號
- **THEN** lifespan shutdown 執行 `await close_opensearch_client()`，log 無 aiohttp `Unclosed client session` warning

#### Scenario: 單次 msearch
- **WHEN** 處理一個 `/search` 請求
- **THEN** 對 OpenSearch 僅發出一次 `_msearch` 請求（含 k-NN 與 BM25 兩個子查詢），而非兩次獨立 `_search`

### Requirement: 測試契約 — 單元純函式 + mock smoke + opt-in 準確度

測試 SHALL 分三層：(1) `tests/test_search_units.py` SHALL 覆蓋 RRF（融合正確性、k 參數、空清單、單邊缺漏、tie-break determinism）、DSL builder 結構、mock 向量不變量、`SearchService` 編排（fake repo 注入），SHALL NOT 需要 docker / 網路 / AWS 憑證；(2) `tests/test_search_api_smoke.py` SHALL 於 mock mode 對 `GET /search` 驗證 200 + 結構 + 融合證據 + 參數邊界 + 空結果 200，需 OpenSearch（Phase 1 資料在位），OpenSearch 不可達時 SHALL skip 而非 fail（對齊 `test_pipeline_e2e.py` 慣例），SHALL NOT 需要 Bedrock；(3) 準確度評估 SHALL 為 opt-in 腳本（`scripts/etl/judge_hybrid_search.py`）：重用 approved golden set（status gate 程式化強制）與 LLM-judge 量尺，同一輪並排 hybrid / k-NN-only / BM25-only 三欄，成功標準為 hybrid 不劣於單一方法且互補性保留（向量強項與 BM25 強項 query 均不歸零），未達 SHALL 如實回報，SHALL NOT 放寬判定。既有測試（`test_etl_units.py` / `test_chains.py` / `test_pipeline_e2e.py` / `test_product_search_units.py`）SHALL 維持全綠。

#### Scenario: 單元測試獨立於基礎設施
- **WHEN** 在 OpenSearch / Postgres 容器全停、無 AWS 憑證的環境執行 `uv run pytest tests/test_search_units.py`
- **THEN** 全部通過，零網路呼叫

#### Scenario: smoke 缺基礎設施時 skip
- **WHEN** OpenSearch 不可達時執行 `uv run pytest tests/test_search_api_smoke.py`
- **THEN** 整模組 skipped（附前置指令提示），非 failed

#### Scenario: 準確度評估 gate
- **WHEN** 對 `meta.status: draft` 的 golden set 執行 `judge_hybrid_search.py`
- **THEN** 立即 exit 1，不發任何 Bedrock 或 OpenSearch 請求

#### Scenario: hybrid 不劣於單一方法
- **WHEN** 對 approved golden set（15 條）完成三欄評估
- **THEN** 報告 Summary 顯示 hybrid 全局相關數 ≥ max(vec-only, bm25-only)，且 q04（ThinkPad，BM25 強項）與情境式 query（向量強項）的 hybrid 相關數均 > 0

### Requirement: 安全契約 — mock 預設、真 embedding 花錢告知、零 DB 影響

`analyzer_mock_mode` 預設 SHALL 維持 `true`，本機開發打 `/search` SHALL NOT 產生 Bedrock 費用。任何 mock OFF 的真 embedding / LLM-judge 執行（含 Phase 7 準確度評估）SHALL 事前告知使用者預估成本並取得同意（safety.md §1）；lab 憑證過期 SHALL 以 `scripts/refresh-lab-creds.sh` 刷新，SHALL NOT 手動編輯 `.env.local`，且任何 log / 報告 / commit SHALL NOT 印出 AWS access key。本變更 SHALL NOT 觸碰 PostgreSQL schema：零 alembic migration（`alembic current` 前後一致、`alembic/versions/` 無新檔）。依賴變動 SHALL 僅限 `opensearch-py` 加 `[async]` extra。

#### Scenario: 花費 gate
- **WHEN** agent 準備執行 Phase 7 準確度評估（真 Bedrock）
- **THEN** 對話中存在「成本估算告知 + 使用者明確同意」紀錄，否則不得執行

#### Scenario: 零 DB 影響
- **WHEN** 比對實作前後的 `alembic current` 與 `alembic/versions/`
- **THEN** revision 相同、無新 migration 檔

#### Scenario: 預設零花費
- **WHEN** 開發者以預設環境（`ANALYZER_MOCK_MODE=true`）啟動 app 並反覆呼叫 `/search`
- **THEN** AWS 帳單零增量（無任何 Bedrock 呼叫）
