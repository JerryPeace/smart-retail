# product-search-vectorization — Tasks

> 排序原則：依賴鏈 前置 → P1-1 docker → P1-2 索引 → P1-3 載入 → 純函式測試（在任何 Bedrock 花費之前）→ P1-4 嵌入（Bedrock 告知 gate）→ golden set 起草 → **使用者審核 gate** → P1-5 驗證 → verification。
>
> 標 **【使用者】** 的 task 是使用者動作，agent 不可代辦、只能催辦。兩個硬 gate：**5.1（Bedrock 花費同意）**、**6.2（golden set 審核）**——gate 未過，後續 task 不得開工。
>
> ⚠️ 本變更零 DB migration、零 `src/recommender/` 改動。做到一半覺得需要 → 停下與使用者確認（代表偏離範圍）。

## Phase 0 — 前置

- [x] **0.1【使用者】** 將來源檔放入 `products/OpenSearch_Full_20260612_030007.json`（目前該目錄為空，**執行期 blocker**：0.1 未完成時 Phase 3 之後全部卡住，但 Phase 1（docker）可先行）。
      ✅ 判準：`ls -lh products/OpenSearch_Full_20260612_030007.json` 顯示約 36MB。
- [x] **0.2** `.gitignore` 加 `products/OpenSearch_Full_*.json`（36MB 不入庫；用 wildcard 容納未來月份 dump）。
      ✅ 判準：`git check-ignore products/OpenSearch_Full_20260612_030007.json` exit 0；`git status` 看不到該檔。
- [x] **0.3** `uv add opensearch-py`（pyproject.toml 目前無此依賴）。
      ✅ 判準：`uv run python -c "import opensearchpy; print(opensearchpy.__version__)"` 正常輸出；`git diff pyproject.toml` 僅新增此一依賴。

## Phase 1 — P1-1 本地 OpenSearch（docker）

- [x] **1.1** 建 `docker/opensearch/Dockerfile`：`FROM opensearchproject/opensearch:<2.19 系列最新 patch tag，查 Docker Hub 釘死>` + `RUN bin/opensearch-plugin install --batch analysis-smartcn`。
      ✅ 判準：`docker build docker/opensearch/` 成功。
- [x] **1.2** `docker-compose.dev.yml` 加 `opensearch` 服務：`build:` 指向 1.1、`container_name: marketing-poc-opensearch`、single-node、`DISABLE_SECURITY_PLUGIN=true`、`DISABLE_INSTALL_DEMO_CONFIG=true`、`OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g`、memlock ulimits、`9200:9200`、`opensearch_data` volume、`app-network`、healthcheck（`curl -sf http://localhost:9200/_cluster/health`，interval 10s / retries 12，對齊既有慣例）。security off 處加註「僅限本地 POC」。
      ✅ 判準：`docker compose -f docker-compose.dev.yml up -d opensearch` 後，`docker inspect --format '{{.State.Health.Status}}' marketing-poc-opensearch` 為 `healthy`；`curl -s localhost:9200/_cluster/health | jq -r .status` 為 `green`。
- [x] **1.3** 驗證 smartcn plugin 生效。
      ✅ 判準：`curl -s localhost:9200/_cat/plugins` 含 `analysis-smartcn`；`curl -s -XPOST localhost:9200/_analyze -H 'Content-Type: application/json' -d '{"analyzer":"smartcn","text":"靈芝保健飲品"}' | jq '[.tokens[].token]'` 切出詞（如「靈芝」「保健」），非逐字單切。
- [x] **1.4**（可選）加 `opensearch-dashboards` 服務（同 tag、`5601:5601`、`DISABLE_SECURITY_DASHBOARDS_PLUGIN=true`、`profiles: [dashboards]`）。
      ✅ 判準：`docker compose --profile dashboards up -d` 後 `curl -sf localhost:5601/api/status` 回 200；預設（無 profile）`docker compose up -d` 不啟動它。

## Phase 2 — P1-2 建 k-NN 索引

- [x] **2.1** 在 `scripts/etl/load_products_os.py` 檔頭定義 `INDEX_SETTINGS` / `INDEX_MAPPING` 常數（design §2 全表：`index.knn=true`、replicas 0、載入期 `refresh_interval=-1`；smartcn text 欄 ×3、keyword 欄、price float、isSearchable integer、`embedding` knn_vector 1024/faiss/hnsw/innerproduct），腳本啟動時 `indices.create`（已存在則跳過）。
      ✅ 判準（建立後）：`curl -s localhost:9200/products_v1/_mapping | jq '.products_v1.mappings.properties.embedding'` 顯示 `dimension: 1024`、`engine: "faiss"`、`space_type: "innerproduct"`、`name: "hnsw"`；`curl -s localhost:9200/products_v1/_settings | jq '.products_v1.settings.index.knn'` 為 `"true"`；`martName` 的 analyzer 為 `smartcn`。

## Phase 3 — P1-3 載入原始資料（依賴 0.1、Phase 1–2）

- [x] **3.1** 完成 `scripts/etl/load_products_os.py`（design §3）：純函式 `detect_format` / `extract_sources`（plain array 與 search-response hits 兩種結構都吃，未知格式 fail fast）→ 過濾 `isSearchable == 1` → `helpers.bulk`（`index` action、`_id=str(martId)`、chunk 500）→ 復原 `refresh_interval="1s"` → 印 summary（總數/過濾數/錯誤數，bulk 錯誤 >0 即 exit 1）。風格對齊 `scripts/etl/aggregate_monthly.py`（docstring、檔頭常數、main guard）。
      ✅ 判準：`uv run python scripts/etl/load_products_os.py` 成功結束；`curl -s localhost:9200/products_v1/_count | jq .count` ≈ 26014（26018 − isSearchable=0 的 4 筆；以實際過濾 log 為準）。
- [x] **3.2** 冪等驗證：再跑一次 3.1 同指令。
      ✅ 判準：第二次跑完 `_count` 與第一次**完全相同**（`index` action + `_id=martId` 覆寫不翻倍）；`refresh_interval` 為 `"1s"`。

## Phase 4 — 撰寫 embed 腳本 + 純函式測試（在任何 Bedrock 花費之前）

- [x] **4.1** 撰寫 `scripts/etl/embed_products_os.py`（design §4，**本 task 只寫不跑**）：must_not exists `embedding` 取缺漏 doc → `build_embed_text` 純函式（martName+feature+keyword+三層 category；strip HTML、`or ""` 防字面 None、空白壓縮、50k truncate）→ boto3 `Session(profile_name="lab", region_name="ap-northeast-1")` per-thread、body `{"inputText", "dimensions": 1024, "normalize": true}` → exponential backoff（429/5xx/Throttling，max 8 次）→ `update` action bulk 寫回（批次 200~500）→ ThreadPoolExecutor 預設 8 workers → `ExpiredTokenException` 時印 `./scripts/refresh-lab-creds.sh` 續跑指引。
      ✅ 判準：`uv run python -c "import importlib.util as u; s=u.spec_from_file_location('m','scripts/etl/embed_products_os.py'); m=u.module_from_spec(s); s.loader.exec_module(m)"` import 成功且**零網路呼叫**（main guard 隔離 IO）。
- [x] **4.2** 撰寫 `tests/test_product_search_units.py`（design §6，對齊 `tests/test_etl_units.py` 慣例，no DB / no network / no docker）：`detect_format`/`extract_sources` 兩種結構 + 未知格式 raise；`build_embed_text` 的 None→""（斷言輸出不含字面 `"None"`）、strip HTML、truncate 邊界；golden set loader 的 schema 與 `status != approved` 拒跑判斷（loader 函式可先於 verify 腳本完成前以同檔或暫置形式存在，最終歸位 `verify_search_os.py`）。腳本模組用 `importlib.util.spec_from_file_location` 載入。
      ✅ 判準：`uv run pytest tests/test_product_search_units.py -v` 全綠，**不需 docker**（可在 `docker compose stop opensearch` 狀態下通過）；既有 `uv run pytest tests/test_etl_units.py` 仍全綠。

## Phase 5 — P1-4 執行向量化（Bedrock 告知 gate）

- [x] **5.1【使用者】＝ GATE** 告知並取得同意後才可執行：本步打**真 AWS Bedrock**（Titan v2，profile `lab`，ap-northeast-1），26,014 筆 ≈ 390 萬 token ≈ **< $0.1 一次性**；P1-5 的 ~20 次 query 嵌入（成本忽略不計）一併授權。預估 wall-clock 10–20 分（8 並發）。
      ✅ 判準：對話中有使用者明確同意紀錄。**未同意前不得跑 5.2。**
- [x] **5.2** 確認 lab 憑證有效（必要時先 `./scripts/refresh-lab-creds.sh`）後執行 `uv run python scripts/etl/embed_products_os.py`。憑證中途過期屬預期：refresh 後重跑同指令即續跑。
      ✅ 判準：`curl -s -XPOST localhost:9200/products_v1/_count -H 'Content-Type: application/json' -d '{"query":{"exists":{"field":"embedding"}}}' | jq .count` == `products_v1/_count` 總數（缺 embedding = 0）。
- [x] **5.3** 續跑機制驗證（若 5.2 一次跑完，用「中途 Ctrl-C 一次再重跑」驗證）：中斷後重跑，log 顯示只處理剩餘缺 embedding 的 doc，不重嵌已完成者。
      ✅ 判準：重跑 log 的「本輪嵌入筆數」< 總數，最終缺漏 = 0；隨機抽一筆 `curl -s localhost:9200/products_v1/_doc/<martId> | jq '.._source.embedding | length'` 為 1024。

## Phase 6 — golden set（agent 起草 → 使用者審核 gate）

- [x] **6.1** agent 從來源 JSON 起草 `scripts/etl/golden_set_product_search.yaml`（design §5.1）：15~20 條、`lexical_overlap` + `non_overlap` 兩類且 **non_overlap ≥ 8 條**、每條含 `query`/`category`/`expected_mart_ids`/`rationale`、`meta.status: draft`。`expected_mart_ids` 逐一以 jq/grep 核實存在於來源檔（不臆造）；含 ≥2 條打在分類污染商品（如葡萄王靈芝王）。non_overlap 條目須 grep 核實 query 關鍵詞**不**出現在預期商品的 martName/feature/keyword（否則歸 lexical_overlap）。
      ✅ 判準：YAML 可被 4.2 的 loader 測試解析；`yq '.queries | length'` 在 15–20；`yq '[.queries[] | select(.category=="non_overlap")] | length'` ≥ 8；每條 expected_mart_ids 的核實指令與結果附在交付訊息。
- [x] **6.2【使用者】＝ GATE** 審核 golden set：逐條看 query 是否像真實查詢、expected_mart_ids 是否合理，增刪改後核可；同時確認成功標準 N=3（design §5.4）是否接受或調整。核可後把 `meta.status` 改 `approved`、填 `approved_by`/`approved_at`。
      ✅ 判準：`yq '.meta.status' scripts/etl/golden_set_product_search.yaml` 為 `approved`。**未 approved 前不得跑 Phase 7（verify 腳本以程式強制 exit 1）。**

## Phase 7 — P1-5 驗證搜尋結果

- [x] **7.1** 完成 `scripts/etl/verify_search_os.py`（design §5.3–5.4）：開頭檢查 `meta.status == approved` 否則 exit 1 → 每條 query 用 Titan v2（`normalize:true`，與 doc 同模型）嵌入 → k-NN top-10 vs BM25 `multi_match`（martName/feature/keyword）top-10 並排 → 對 expected_mart_ids 算 hit@10 → 固定加跑分類污染示範（category filter 漏葡萄王 vs 向量找到）→ 輸出 `out/search_eval_{YYYYMMDD}.md`（並排表 + Summary）。
      ✅ 判準：對 `status: draft` 的 YAML 跑會 exit 1 並印提示（gate 程式化驗證）；approved 後 `uv run python scripts/etl/verify_search_os.py` 產出報告檔。
- [x] **7.2** 執行驗證並判讀成功標準。
      ✅ 判準：`out/search_eval_*.md` 存在且包含 (a) 每條 query 的向量/BM25 並排 top-10 與 hit@10、(b) Summary 的 vector-only wins 計數 **≥ 3**（或 6.2 調整後的 N）、(c) `lexical_overlap` 類 BM25 hit 率正常（對照組非稻草人）、(d) 分類污染示範段落（filter 漏 / 向量中）。若 < N：如實回報、與使用者討論（調 golden set 或記錄為 POC 負結果），**不得**為湊數改寬判定。

## Phase 8 — Verification（總驗）

- [x] **8.1** 全套測試與冪等終驗：`uv run pytest` 全綠（含既有測試，e2e 前置 `docker compose up -d postgres`）；重跑 `load_products_os.py` 後 `_count` 不變且 embedding 不掉（`index` 覆寫後缺 embedding 數應為 0 或由 embed 續跑補齊——把實測結果記進報告）。
      ✅ 判準：pytest exit 0；load 重跑前後 `_count` 與 exists-embedding count 的四個數字記錄在交付訊息。
- [x] **8.2** 範圍與安全終驗：
      ✅ 判準：`git status` 無 36MB 來源檔；`alembic current` 與開工前相同、`alembic/versions/` 無新檔；`git diff --stat` 只含 docker/opensearch/、docker-compose.dev.yml、.gitignore、pyproject.toml/uv.lock、scripts/etl/ 三支新腳本 + golden set、tests/ 一個新測試檔；log 與報告無 AWS key。
- [x] **8.3** 交付摘要給使用者：載入筆數、嵌入覆蓋率、實際 Bedrock 花費量級、golden set 驗證結論（成功標準達成與否）、`out/search_eval_*.md` 路徑、Phase 2 建議（hybrid/RRF 與模型 benchmark 的銜接點）。
      ✅ 判準：摘要送出且引用具體數字（非「完成了」一句話）。
