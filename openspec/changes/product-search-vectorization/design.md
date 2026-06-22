# product-search-vectorization — Design

> 對齊 `docs/plans/product-search-vectorization.md` 的設計決策 D1–D8 與工作項 P1-1 ~ P1-5。本文件把已審定決策落成可實作規格，不重新開題。

## 0. 設計總綱

- **Simplicity First / POC scripts only**：本次產物是 docker 服務定義 + 三支 `scripts/etl/` 腳本 + 一份 golden set + 純函式測試。**不建 service / repository 層、不接 FastAPI** —— 那是 Phase 2 的事（計劃 §5）。
- **ETL First, LLM Last**：載入（P1-3）純演算法；Bedrock 只做 embedding（原子操作，boto3 直呼，D7/D8），不讓 LLM 解析或計算任何東西。
- **冪等與續跑是一等公民**：`_id=martId`（D5）讓 bulk index 重跑不翻倍；embedding 續跑只補缺。lab 憑證會過期（1–12 小時），「一次跑不完」是預期行為而非錯誤。
- **腳本風格對齊既有 `scripts/etl/`**（`aggregate_monthly.py` 等）：module docstring 寫明輸入/輸出/用法、常數集中在檔頭、純函式與 IO 分離、`if __name__ == "__main__":` 進入點、`uv run python scripts/etl/xxx.py` 執行。

## 1. P1-1 — docker OpenSearch 服務定義要點

加進 `docker-compose.dev.yml`（對齊既有 postgres/redis 的 container_name、network、healthcheck 慣例）：

| 項目 | 規格 | 依據 |
|------|------|------|
| image | OpenSearch **2.19 系列**（實作時釘選 Docker Hub 最新 patch tag，如 `2.19.2`；禁用 `latest`） | D1：2.19 有 faiss + RRF score-ranker（Phase 2 用），避開 3.x 邊角與 nmslib 棄用 |
| smartcn plugin | **自建小 Dockerfile**（`docker/opensearch/Dockerfile`）：`FROM opensearchproject/opensearch:<tag>` + `RUN bin/opensearch-plugin install --batch analysis-smartcn`，compose 用 `build:` 指向 | D6。plugin 版本必須與 OpenSearch 版本完全一致，build 進 image 比 entrypoint 動態裝更確定、重啟不重裝 |
| mode | `discovery.type=single-node` | 26k 筆單節點輕鬆（向量約 106MB） |
| security | `DISABLE_SECURITY_PLUGIN=true`、`DISABLE_INSTALL_DEMO_CONFIG=true` | **僅限本地 POC**（見 §7 safety），免去 cert/帳密讓 curl 與 opensearch-py 連線最簡 |
| JVM | `OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g` | 計劃 §7：1~2GB 足夠 |
| memlock | `bootstrap.memory_lock=true` + `ulimits.memlock: {soft: -1, hard: -1}` | OpenSearch 官方建議，避免 swap |
| ports | `9200:9200`（既有占埠 5434/6380/4567/8081，9200 可用） | repo 現況已核實 |
| healthcheck | `curl -sf http://localhost:9200/_cluster/health \|\| exit 1`，`interval: 10s, timeout: 5s, retries: 12`（冷啟動較慢，給足 retries） | 對齊既有服務 healthcheck 慣例 |
| volume | `opensearch_data:/usr/share/opensearch/data` | 載入 26k + 向量後不想每次重來 |
| container_name | `marketing-poc-opensearch`，掛 `app-network` | 既有命名慣例 |
| dashboards（可選） | `opensearchproject/opensearch-dashboards:<同 tag>`，`5601:5601`、`DISABLE_SECURITY_DASHBOARDS_PLUGIN=true`、`OPENSEARCH_HOSTS=["http://opensearch:9200"]`，放 `profiles: [dashboards]` | 對齊既有「optional services 走 profiles」慣例（api 服務同模式），預設不啟省記憶體 |

Linux 主機需 `vm.max_map_count=262144`；Docker Desktop for Mac 內建已滿足（本機環境），僅在 README/docstring 註記，不做自動化。

## 2. P1-2 — `products_v1` index settings / mapping

### settings

```jsonc
{
  "settings": {
    "index.knn": true,                // 建索引時就要定，不可熱改（見 spec 索引契約）
    "number_of_shards": 1,
    "number_of_replicas": 0,          // 單節點；載入期與常駐皆 0
    "refresh_interval": "-1"          // 僅載入期；P1-3 完成後復原為 "1s"
  }
}
```

### mapping 欄位表（明確宣告下列欄位；來源其餘欄位走 dynamic 預設，POC 不鎖）

| 欄位 | type | analyzer / 參數 | 說明 |
|------|------|----------------|------|
| `martId` | `keyword` | — | 商品編號；同時作 `_id`（D5） |
| `martName` | `text` | `analyzer: smartcn` | 商品名，嵌入文字成分（D4/D6） |
| `feature` | `text` | `analyzer: smartcn` | 商品特色（含 HTML，原文入索引、嵌入前才清洗） |
| `keyword` | `text` | `analyzer: smartcn` | 7% 空值 |
| `categoryLevel1Name` | `keyword` | — | 含分類污染（品牌名），保留原樣供 P1-5 示範 |
| `categoryLevel2Name` | `keyword` | — | 同上（行銷標籤） |
| `categoryLevel3Name` | `keyword` | — | |
| `brand` | `keyword` | — | |
| `price` | `float` | — | |
| `isSearchable` | `integer` | — | 載入已過濾 =1，欄位保留供查證 |
| `embedding` | `knn_vector` | `dimension: 1024`，`method: {engine: "faiss", name: "hnsw", space_type: "innerproduct"}` | D2/D3：Titan v2 `normalize:true` 下單位向量 innerproduct 等價 cosine |

BM25 對照組（P1-5）的 `match` 查詢打 `martName`/`feature`/`keyword`（smartcn 斷詞），確保對照組不是被爛分詞拖垮的稻草人（D6：不裝 smartcn 則 standard analyzer 把中文切單字，比較不公平）。

索引建立方式：直接在 P1-3 載入腳本內建索引（`indices.create`，已存在則跳過）—— 不另寫一支「建索引腳本」，但 mapping/settings 以**模組層級常數 dict** 寫在 `load_products_os.py` 檔頭，可獨立 review。

## 3. P1-3 — `scripts/etl/load_products_os.py`

```
讀 products/OpenSearch_Full_20260612_030007.json (36MB 單行 JSON array)
  → 結構探測：頂層第一元素是「商品物件」還是含 _index/_id/_source 的 search-response hit？
  → 統一抽出 source dict（純函式 extract_sources(raw) → list[dict]）
  → 過濾 isSearchable == 1（預期排除 4 筆 → 26,014）
  → opensearch-py helpers.bulk，action="index"、_id=str(martId)、chunk 500
  → 完成後復原 refresh_interval="1s"，refresh + _count 驗證
```

設計要點：

- **結構探測是純函式**（`detect_format` / `extract_sources`）：計劃 §2 已標注 ⚠️ 須先確認來源是 `_source` 物件陣列還是 search response —— 腳本不賭格式，兩種都吃，其他格式直接 fail fast 報錯（不 fallback LLM，這是結構化 JSON，演算法 first）。
- **冪等**：`index` action + `_id=martId`，重跑 = 覆寫同 doc，`_count` 不變（D5；spec 載入冪等契約）。
- **36MB 一次 `json.load` 進記憶體可接受**（POC、單機），不做 streaming parser —— Simplicity First。
- 連線 `OpenSearch(hosts=["http://localhost:9200"])`，host/port 走檔頭常數（security off 無認證）。
- 結尾印 summary：總筆數、過濾掉幾筆、bulk 錯誤數（>0 即 exit 1）。

## 4. P1-4 — `scripts/etl/embed_products_os.py`

```
查 OpenSearch：bool must_not exists "embedding"（續跑機制：天然只補缺）
  → 取 martId + 嵌入所需文字欄（search_after / scroll 分頁）
  → build_embed_text 純函式組裝 + 清洗（D4）
  → ThreadPoolExecutor 5~10 workers 並發呼叫 Bedrock invoke_model
  → 每 200~500 筆 bulk update 寫回 embedding
  → 迴圈直到 must_not exists 查無 doc
```

設計要點：

- **Bedrock client**：`boto3.Session(profile_name="lab", region_name="ap-northeast-1")` → `client("bedrock-runtime")`，model `amazon.titan-embed-text-v2:0`（計劃 §3 已驗證可用）。每個 worker thread 各自建 client（boto3 client 跨執行緒共用有風險，session-per-thread 最穩）。
- **嵌入文字組裝（D4，純函式 `build_embed_text(doc) -> str`）**：`martName` + `feature` + `keyword` + 三層 `categoryLevelXName`，以換行串接。清洗規則：
  - `feature` strip HTML（regex `<[^>]+>` → 空白即可，POC 不引入 bs4）
  - 每個欄位取值用 `doc.get(field) or ""` —— **防 `None` 被串成字面 `"None"`**（計劃 P1-4 明定）
  - 全形/連續空白壓縮、strip
  - truncate 至 **50,000 字元**（Titan v2 上限 8,192 token / 50k chars 的保守界）
- **request body**：`{"inputText": text, "dimensions": 1024, "normalize": true}` —— `normalize:true` 是 D2 innerproduct≡cosine 的前提，**不可省略**（spec 嵌入契約）。
- **retry**：自寫 exponential backoff（`base 1s, factor 2, max 8 次, 加 jitter`），針對 `ThrottlingException` / HTTP 429 / 5xx；其他例外（如 ValidationException）fail fast 不重試。
- **寫回**：`helpers.bulk` 用 `update` action（`doc: {"embedding": [...]}`），批次 200~500。
- **續跑**：不維護自己的進度檔 —— 「無 `embedding` 欄」本身就是進度狀態，中斷後重跑同一指令即續跑。憑證過期（`ExpiredTokenException`）時印明確指引：`./scripts/refresh-lab-creds.sh` 後重跑。
- **並發**：5~10 workers（檔頭常數，預設 8），尊重 Bedrock RPM quota；估時序列 1~1.5 小時，並發後 ~10–20 分鐘。
- **成本**：~390 萬 token × Titan v2 ≈ **< $0.1 一次性**。執行前的告知義務見 §7。
- 結尾印 summary：本輪嵌入筆數、剩餘缺 embedding 筆數（0 = 完成）、retry 次數。

## 5. golden set + P1-5 驗證

### 5.1 golden set 檔案格式

路徑：`scripts/etl/golden_set_product_search.yaml`（**進 git** —— Phase 2 模型 benchmark 複用，計劃 P1-5 明定）。

```yaml
# 由 agent 從 26k 商品資料起草、使用者審核後生效（審核紀錄見下）
meta:
  status: draft          # draft → approved（使用者審核通過後改）
  approved_by: null      # 審核通過後填使用者
  approved_at: null
  source_file: OpenSearch_Full_20260612_030007.json
queries:
  - id: q01
    query: "靈芝保健飲"
    category: lexical_overlap        # 詞面重疊：BM25 也該行
    expected_mart_ids: ["123456"]    # 從來源資料實際撈出的 martId
    rationale: "詞面直接命中葡萄王靈芝王品名"
  - id: q11
    query: "增強免疫力的飲料"
    category: non_overlap            # 詞面不重疊：BM25 應失敗、向量應成功
    expected_mart_ids: ["123456", "234567"]
    rationale: "語意對應靈芝/人蔘飲品，品名與 feature 無『免疫』字樣（已 grep 核實）"
```

規格：15~20 條；兩類皆有、**`non_overlap` ≥ 8 條**（成功標準的分母在這類）；`expected_mart_ids` 必須是來源檔實際存在的 martId（起草時 grep/jq 核實，不臆造）；每條附 `rationale` 供使用者審核判斷。

### 5.2 起草與審核流程（已拍板：agent 起草、使用者審核）

1. agent 讀來源 JSON，挑代表性商品（含 ≥2 條打在分類污染商品上，如葡萄王靈芝王），起草 YAML（`status: draft`）。
2. **使用者審核 gate**：使用者逐條看 query 是否像真實查詢、expected_mart_ids 是否合理，可增刪改。通過後 `status: approved`。
3. **`status != approved` 時 `verify_search_os.py` 直接 exit 1 拒跑** —— gate 用程式強制，不靠自覺。

### 5.3 P1-5 — `scripts/etl/verify_search_os.py`

每條 query：

1. 同一 Titan v2 + `normalize:true` 嵌入 query（**query/doc 同模型同維度同 normalize**，spec 嵌入契約；~20 次呼叫成本忽略不計，但仍是真 Bedrock，併入 §7 告知）。
2. **k-NN query**：`{"knn": {"embedding": {"vector": [...], "k": 10}}}` 取 top-10。
3. **BM25 對照組**：`multi_match` 打 `martName`/`feature`/`keyword` 取 top-10。
4. 對照 `expected_mart_ids` 算每邊 hit@10。
5. **分類污染示範**（固定加跑）：「保健食品」類查詢 + `categoryLevel1Name` filter（漏掉葡萄王 —— category=品牌名）vs 純向量（找到）。

### 5.4 輸出格式

寫 `out/search_eval_{YYYYMMDD}.md`（沿用 `out/` 慣例）：

```markdown
## q11「增強免疫力的飲料」(non_overlap)
| rank | 向量 top-10           | BM25 top-10 |
|------|----------------------|-------------|
| 1    | ✅ 123456 葡萄王靈芝王 | 987654 不相關商品 |
...
判定：vector hit@10 = 2/2，bm25 hit@10 = 0/2 → **vector-only win**

## Summary
- non_overlap 共 N 條：vector-only wins = X（成功標準 ≥ 3）
- lexical_overlap 共 M 條：BM25 hit 率（健全性檢查，兩邊都該行）
- 分類污染示範：category filter 漏 K 筆 / 向量找到
```

**成功標準（量化）**：`non_overlap` 類中「向量 hit@10 ≥1 且 BM25 hit@10 = 0」的查詢 **≥ 3 條**（計劃寫「≥ N 個」未定值；本設計取 N=3，golden set 審核時可與使用者一併調整）。另要求 `lexical_overlap` 類 BM25 hit 率正常（證明對照組不是稻草人）。

## 6. 測試策略（已拍板：只測純函式）

新增 `tests/test_product_search_units.py`，對齊 `tests/test_etl_units.py` 慣例（module docstring 標明 no DB / no network / no Docker）。

| 純函式 | 所在 | 測什麼 |
|--------|------|--------|
| `detect_format` / `extract_sources` | `load_products_os.py` | 兩種 JSON 結構（plain array vs search-response hits）都解出 source list；未知格式 raise |
| `build_embed_text` | `embed_products_os.py` | 欄位組裝順序、`keyword=None` → `""`（不出現字面 `"None"`）、strip HTML、空白壓縮、50k truncate |
| `strip_html` / `truncate` 等子函式 | `embed_products_os.py` | 邊界：空字串、純 HTML、超長輸入 |
| golden set loader / `status==approved` gate 判斷 | `verify_search_os.py` | YAML schema 驗證、draft 拒跑判斷 |

- **import 方式**：scripts 非 package，測試以 `importlib.util.spec_from_file_location` 載入腳本模組（腳本必須有 `if __name__ == "__main__":` guard，import 不觸發 IO）。
- **不測**：OpenSearch I/O、Bedrock I/O、retry/並發行為（I/O 驗證走 tasks 的 curl/_count 手動判準；已拍板）。

## 7. Safety（對齊 `.claude/rules/safety.md`）

| 風險 | 對策 |
|------|------|
| **P1-4 打真 Bedrock 花錢** | 估算 ~390 萬 token ≈ **< $0.1 一次性**。雖遠低於「批次 100+ prompt」門檻，仍是真錢真呼叫：**執行 `embed_products_os.py` 前必須明確告知使用者預估成本並取得同意**（tasks 設 gate）。P1-5 的 ~20 次 query 嵌入併入同一次告知 |
| **lab 憑證過期（1–12 小時）** | embed 一次跑不完是預期行為。腳本捕捉 `ExpiredTokenException` → 印 `./scripts/refresh-lab-creds.sh` 指引 → 重跑即續跑（must_not exists 機制）。不在 log / 報告印 AWS key |
| **本地 security off** | `DISABLE_SECURITY_PLUGIN=true` **僅限本地 POC**，docker-compose 註解明示；任何上 prod 討論回到計劃 §7 的遷移評估 |
| **36MB 來源檔** | 不入 git：`.gitignore` 加 `products/OpenSearch_Full_*.json`。商品目錄非 PII，但仍是公司內部資料，不外流 |
| **本地 OpenSearch 資料** | 純本地 docker volume，delete / 重建安全（等同 LocalStack 級別）；`docker compose down -v` 仍須先警告（會連 postgres dev 資料一起掉 —— 既有 safety 規則） |
| **零 DB / migration 影響** | 本次不碰 PostgreSQL、不產生 alembic migration；`alembic current` 前後一致 |
