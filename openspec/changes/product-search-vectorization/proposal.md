# product-search-vectorization

> 上游計劃：`docs/plans/product-search-vectorization.md`（已審定，D1–D8 決策已定）。本 OpenSpec 是該計劃 **Phase 1（P1-1 ~ P1-5）的規格化與任務化**，不重新設計；內容若有衝突，以計劃文件為準。

## Why

本公司 26,018 筆商品目錄存在**分類污染**：≥363 筆品牌旗艦館商品（葡萄王、台塑生醫、順天本草、sakuyo、MEGA KING、大葉高島屋）把品牌名塞進 `categoryLevel1`，`categoryLevel2` 混入「成分分類／熱銷活動」等行銷標籤。後果是：

1. **category filter 會漏商品** — 搜「保健食品」+ category 篩選會漏掉葡萄王靈芝王（它的 category 是品牌名「葡萄王」，不是「保健」）。下游任何依賴分類的推薦／搜尋都繼承這個洞。
2. **中文 BM25 撐不起語意查詢** — 來源資料 100% 繁體中文，「增強免疫力的飲料」這種詞面不重疊查詢，關鍵字比對找不到靈芝飲品。

**POC 商業主張**：語意搜尋（向量）從 `martName`/`feature` 抓意義、不依賴髒分類，能找到 BM25 與 category filter 找不到的商品。本變更要在**本地 docker OpenSearch + Bedrock Titan v2** 上把這件事**量化驗證**出來（golden set 並排比較），而不是只跑一個 demo。

**定位**：POC，不綁定上 prod。實作盡量照 AWS 最佳實踐（OpenSearch 2.19.x、faiss、innerproduct），版本不受 prod 牽制。

## What Changes

對齊計劃文件工作項編號 P1-1 ~ P1-5，加上本次拍板的兩個流程決定：

- **P1-1 本地 OpenSearch（docker）** — `docker-compose.dev.yml` 加 `opensearch` 服務（2.19.x、single-node、security off、JVM 1g、healthcheck、smartcn plugin），埠 9200；Dashboards 5601 為可選 profile。對齊既有服務的 healthcheck 慣例。
- **P1-2 建 k-NN 索引 `products_v1`** — `index.knn=true`，文字欄走 smartcn analyzer，`embedding` 為 `knn_vector`（1024 維、faiss、hnsw、innerproduct，對齊 D2/D3）。
- **P1-3 載入原始資料** — 新增 `scripts/etl/load_products_os.py`：先探測 JSON 結構 → 過濾 `isSearchable=1` → bulk index（`_id=martId`，D5，天然冪等）。純演算法，零 LLM（ETL First）。
- **P1-4 Titan v2 向量化** — 新增 `scripts/etl/embed_products_os.py`：boto3 lab profile 直呼 Bedrock（D7/D8），文字清洗 → 批次嵌入 → bulk update 寫回；retry/backoff、續跑機制（只補無 embedding 的 doc）、5~10 並發。**打真 Bedrock（~$0.1 一次性），執行前必須告知使用者（safety gate）**。
- **golden set：agent 起草、使用者審核**（已拍板）— 實作時由 agent 從 26k 商品資料起草 15~20 條查詢（詞面重疊 + 詞面不重疊兩類）+ 預期命中清單，存成結構化 YAML（query/category/expected_mart_ids/rationale）。**使用者審核通過前不得跑 P1-5 驗證**（tasks 設明確 gate）。
- **P1-5 驗證搜尋結果** — 新增 `scripts/etl/verify_search_os.py`：每條查詢以同一 Titan v2 嵌入 → k-NN query vs BM25 `match` 並排比較 top-10，外加 category filter 分類污染示範。成功標準：詞面不重疊查詢中「向量找到、BM25 找不到」≥ 3 例。輸出可留存的比較報告，golden set 供 Phase 2 benchmark 複用。
- **純函式測試**（已拍板）— 只測 deterministic 純函式：文字清洗（strip HTML、`keyword` 空值處理、truncate）、JSON 結構解析、嵌入文字組裝。對齊 `tests/test_etl_units.py` 慣例，無網路、無 docker。**OpenSearch / Bedrock I/O 不測。**
- **前置雜項** — `.gitignore` 加來源檔規則（36MB 不入庫）；`uv add opensearch-py`；來源檔 `products/OpenSearch_Full_20260612_030007.json` 由**使用者**放入（目前不存在，是執行期 blocker）。

## Out of Scope（本次明確不做）

承計劃文件 §6 全部，加上本 OpenSpec 補充項：

| 項目 | 為什麼不做 |
|------|-----------|
| 上 prod、複製 prod 的 RDS→event→OpenSearch 同步 | POC 不綁 prod；JSON 直接載入本地 OpenSearch（計劃 §6） |
| pgvector | 既然要練 OpenSearch 生態，直接用 OpenSearch（計劃 §6） |
| Bedrock Knowledge Base | RAG 文件問答抽象，非商品排序搜尋，錯抽象（計劃 §6） |
| OpenSearch Bedrock connector（方式 B） | 本地設定重；POC 用方式 A boto3 自嵌（D7，計劃 §6） |
| Hybrid 融合（RRF）/ API endpoint / 三層架構接線 | **Phase 2**。本次只有 POC scripts，不建 `repositories/product_search_repo.py`、`services/search_service.py`、`api/search.py`（計劃 §5 Phase 2 才有三層） |
| 修分類污染（FM 重新分類 363 筆品牌館商品） | **Phase 3**（計劃 §5） |
| 預先囤備用向量、多模型比較（Titan vs Cohere） | Phase 1 單 Titan v2；中文模型 benchmark 留 Phase 2 用 golden set 測 recall@10（D3，計劃 §6） |
| LangChain 包 embedding | embedding 是原子操作，boto3 直呼（D8）；LangChain 留 Phase 3 |
| OpenSearch / Bedrock I/O 的自動化測試 | 已拍板只測純函式；I/O 驗證走 tasks 的手動判準（curl/_count） |
| 把商品資料寫進 PostgreSQL / 新增 alembic migration | 本次資料只進 OpenSearch，零 DB schema 變更 |
| Bedrock Batch Inference | 半價但流程重；POC 量級用 on-demand 合理（計劃 P1-4 已記錄此取捨） |
