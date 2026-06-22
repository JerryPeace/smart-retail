# Spec: product-search-vectorization

本規格定義 Phase 1（P1-1 ~ P1-5）完成後必須成立的六份契約：索引契約、載入冪等契約、嵌入契約、驗證契約、安全契約、測試契約。實作完成後，任何違反下列 Requirement 的產物視為未通過驗收。對齊 `docs/plans/product-search-vectorization.md` 決策 D1–D8。

## ADDED Requirements

### Requirement: 索引契約 — `products_v1` mapping 一次定對，knn 不可熱改

`products_v1` SHALL 以 `index.knn=true` 建立；`embedding` 欄 SHALL 為 `knn_vector`，`dimension=1024`、`method={engine: faiss, name: hnsw, space_type: innerproduct}`（D1/D2/D3）；`martName`/`feature`/`keyword` SHALL 為 `text` 且 `analyzer=smartcn`（D6）；`categoryLevelXName`/`brand` 為 `keyword`、`price` 為 `float`、`isSearchable` 為 `integer`。因 `index.knn` 與 `knn_vector` mapping 無法對既有索引熱改，任何 mapping 變更 SHALL 走「建新索引（如 `products_v2`）+ reindex + alias 切換」，SHALL NOT 嘗試原地修改。

#### Scenario: mapping 驗收
- **WHEN** 執行 `curl -s localhost:9200/products_v1/_mapping`
- **THEN** `embedding` 顯示 `dimension: 1024`、`engine: "faiss"`、`name: "hnsw"`、`space_type: "innerproduct"`，且 `martName`/`feature`/`keyword` 的 analyzer 為 `smartcn`

#### Scenario: smartcn 斷詞生效
- **WHEN** 以 `_analyze` API 用 `smartcn` analyzer 分析「靈芝保健飲品」
- **THEN** 產出詞級 token（如「靈芝」「保健」），非逐字單切 —— 確保 BM25 對照組（P1-5）不是被爛分詞拖垮的稻草人

#### Scenario: 需要改 mapping 時
- **WHEN** 發現 `embedding` 維度、engine 或任何欄位型別需要變更
- **THEN** 建新版索引 + `_reindex` + alias 切換，不對 `products_v1` 原地改 mapping

### Requirement: 載入冪等契約 — 重跑 `_count` 不變

`load_products_os.py` SHALL 以 `_id=str(martId)`（D5）、bulk `index` action 載入，使重跑為覆寫而非新增；SHALL 過濾 `isSearchable != 1` 的商品（預期排除 4 筆）；SHALL 對來源 JSON 先做結構探測（plain array vs search-response hits 兩種皆支援），未知結構 SHALL fail fast 報錯而非猜測或 fallback LLM（結構化 JSON 屬演算法範疇）。載入完成 SHALL 復原 `refresh_interval`。

#### Scenario: 冪等重跑
- **WHEN** 連續執行兩次 `uv run python scripts/etl/load_products_os.py`
- **THEN** 兩次結束後 `GET products_v1/_count` 數值完全相同（≈26,014），文件數不翻倍

#### Scenario: 過濾不可搜尋商品
- **WHEN** 來源檔含 `isSearchable=0` 的商品
- **THEN** 該商品不出現在索引中，且過濾筆數印在 summary log

#### Scenario: 未知 JSON 結構
- **WHEN** 來源檔頂層結構既非商品物件陣列、也非含 `_source` 的 search-response
- **THEN** 腳本以非零 exit code 終止並印出實際偵測到的結構，不寫入任何文件

### Requirement: 嵌入契約 — normalize:true、query/doc 同模型、續跑只補缺

所有 embedding SHALL 由 `amazon.titan-embed-text-v2:0` 產生，request body SHALL 含 `"dimensions": 1024` 與 `"normalize": true` —— normalize 是 innerproduct 等價 cosine（D2）的前提，SHALL NOT 省略。P1-5 的查詢端嵌入 SHALL 使用與文件端完全相同的模型、維度與 normalize 設定。嵌入文字 SHALL 由 `build_embed_text` 純函式組裝（D4：martName+feature+keyword+三層 categoryName），清洗規則：strip HTML、欄位空值以 `or ""` 處理（輸出不得含字面 `"None"`）、truncate 至 50,000 字元。`embed_products_os.py` SHALL 以「`embedding` 欄不存在」作為唯一進度狀態：重跑只補缺、不重嵌已完成文件、不維護額外進度檔。

#### Scenario: 全量覆蓋
- **WHEN** embed 腳本（可能歷經多次續跑）最終完成
- **THEN** `exists embedding` 的 `_count` 等於索引總 `_count`，且隨機抽查文件的 `embedding` 長度為 1024

#### Scenario: 中斷續跑
- **WHEN** 嵌入中途中斷（Ctrl-C 或 lab 憑證過期）後重跑同一指令
- **THEN** 腳本只處理仍缺 `embedding` 的文件，log 的本輪處理筆數小於總數，已有向量的文件不被重新呼叫 Bedrock

#### Scenario: 空值欄位不污染嵌入文字
- **WHEN** 商品的 `keyword` 為 null / 缺欄（來源約 7%）
- **THEN** `build_embed_text` 輸出不含字面字串 `"None"`，該欄位以空字串略過

#### Scenario: 限流重試
- **WHEN** Bedrock 回 ThrottlingException / 429 / 5xx
- **THEN** exponential backoff 重試（上限 8 次）；ValidationException 等非暫時性錯誤 fail fast 不重試

### Requirement: 驗證契約 — golden set 兩類查詢 + 量化成功標準

golden set（`scripts/etl/golden_set_product_search.yaml`）SHALL 含 15~20 條查詢、分 `lexical_overlap`（詞面重疊，BM25 也該行）與 `non_overlap`（詞面不重疊，BM25 應失敗、向量應成功）兩類且 non_overlap ≥ 8 條；每條 SHALL 含 `query`/`category`/`expected_mart_ids`/`rationale`，`expected_mart_ids` SHALL 是來源檔實際存在的 martId。golden set SHALL 由 agent 起草（`meta.status: draft`）、**使用者審核通過**（`status: approved`）後方可用於驗證 —— `verify_search_os.py` SHALL 在 `status != approved` 時以非零 exit code 拒跑。驗證 SHALL 對每條查詢並排比較 k-NN top-10 與 BM25 `multi_match`（martName/feature/keyword）top-10，並固定加跑分類污染示範（category filter vs 向量）。成功標準：non_overlap 類中「向量 hit@10 ≥ 1 且 BM25 hit@10 = 0」的查詢 ≥ 3 條（N=3 為預設，得於審核 gate 與使用者調整）。

#### Scenario: 審核 gate 程式化強制
- **WHEN** 對 `meta.status: draft` 的 golden set 執行 `verify_search_os.py`
- **THEN** 腳本立即 exit 1 並提示需使用者審核，不發出任何 Bedrock 或 OpenSearch 查詢

#### Scenario: 語意搜尋價值量化
- **WHEN** 對 approved golden set 跑完整驗證
- **THEN** 產出 `out/search_eval_{YYYYMMDD}.md`，Summary 含 vector-only wins 計數；計數 ≥ N（預設 3）即達成功標準；未達 SHALL 如實回報為負結果，SHALL NOT 放寬判定湊數

#### Scenario: 對照組健全性
- **WHEN** 檢視 lexical_overlap 類結果
- **THEN** BM25 hit@10 表現正常（兩邊都找得到），證明對照組未被分詞或查詢設計弱化

#### Scenario: 分類污染示範
- **WHEN** 對「保健食品」類查詢加 `categoryLevel1Name` filter 與純向量搜尋並排
- **THEN** 報告顯示 filter 漏掉品牌館商品（如 category=「葡萄王」的靈芝王）而向量找到 —— 量化支撐 POC 商業主張

#### Scenario: golden set 可複用
- **WHEN** Phase 2 進行模型 benchmark（Titan vs Cohere）
- **THEN** 同一份 approved YAML 可直接作為 recall@10 測試集（檔案在 git 版控內）

### Requirement: 安全契約 — Bedrock 告知、憑證處理、security off 僅限 POC

執行 `embed_products_os.py`（打真 Bedrock，~390 萬 token ≈ <$0.1）前 SHALL 明確告知使用者預估成本並取得同意（tasks 5.1 gate）；P1-5 的查詢嵌入併入同一授權。lab 臨時憑證過期 SHALL 以 `scripts/refresh-lab-creds.sh` 刷新後續跑，SHALL NOT 手動編輯 `.env.local` 的 key，且任何 log / 報告 / commit SHALL NOT 印出 AWS access key。`DISABLE_SECURITY_PLUGIN=true` SHALL 僅用於本地 POC docker，compose 檔 SHALL 加註此限制。36MB 來源檔 SHALL 被 `.gitignore` 排除，SHALL NOT 進入 git 歷史。本變更 SHALL NOT 觸碰 PostgreSQL schema（零 alembic migration）。

#### Scenario: 花費 gate
- **WHEN** agent 準備首次執行 embed 腳本
- **THEN** 對話中存在「成本估算告知 + 使用者明確同意」紀錄，否則不得執行

#### Scenario: 憑證過期
- **WHEN** 嵌入途中 boto3 拋 ExpiredTokenException
- **THEN** 腳本印出 `./scripts/refresh-lab-creds.sh` 指引後結束；刷新後重跑即從缺漏處續跑，已花費的嵌入不重複付費

#### Scenario: 來源檔不入庫
- **WHEN** 來源檔就位後執行 `git status`
- **THEN** `products/OpenSearch_Full_*.json` 不出現在 untracked 清單（`git check-ignore` 命中）

#### Scenario: 零 DB 影響
- **WHEN** 比對實作前後的 `alembic current` 與 `alembic/versions/`
- **THEN** revision 相同、無新 migration 檔

### Requirement: 測試契約 — 只測純函式，無網路無 docker

`tests/test_product_search_units.py` SHALL 存在且覆蓋：JSON 結構探測（兩種格式 + 未知格式 raise）、`build_embed_text`（None→""、strip HTML、truncate 邊界）、golden set loader 與 approved gate 判斷。測試 SHALL NOT 需要 docker、網路或 AWS 憑證（對齊 `tests/test_etl_units.py` 慣例）。OpenSearch / Bedrock I/O SHALL NOT 有自動化測試（已拍板；I/O 驗證走 tasks 的 curl/_count 手動判準）。三支腳本 SHALL 以 `if __name__ == "__main__":` 隔離 IO，使測試可安全 import 純函式。

#### Scenario: 測試獨立於基礎設施
- **WHEN** 在 OpenSearch 容器停止、無 AWS 憑證的環境執行 `uv run pytest tests/test_product_search_units.py`
- **THEN** 全部通過，過程零網路呼叫

#### Scenario: import 腳本不觸發 IO
- **WHEN** 測試以 importlib 載入 `load_products_os.py` / `embed_products_os.py` / `verify_search_os.py`
- **THEN** 模組載入本身不連 OpenSearch、不建 boto3 client、不讀來源大檔

#### Scenario: 既有測試不受影響
- **WHEN** 執行 `uv run pytest tests/test_etl_units.py`
- **THEN** 仍全綠（本變更不動 `src/recommender/` 任何檔案）
