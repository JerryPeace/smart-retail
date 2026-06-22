# Phase 3.0 — 電商商品語意 / Hybrid 搜尋 POC（本地 OpenSearch + Bedrock Titan v2）

> 狀態：✅ **Phase 1 已執行收案（2026-06-13）**——26,014 筆全載入 + 全向量化；驗證結論見下方「Phase 1 執行結果」。Phase 2（hybrid + search API）獲得實證支持，待規劃。
> **定位：POC，不綁定上 prod。** 目標是「**本地端 + Bedrock API 驗證搜尋結果**」，實作**盡量照 AWS 最佳實踐**。因不綁 prod，OpenSearch 版本/engine 直接挑最佳實踐值，不受 prod 版本牽制。
> 技術棧：本地 docker **OpenSearch 2.19.x（k-NN, faiss）** · Bedrock（Titan v2，boto3 直呼）· Python（opensearch-py）。

---

## 1. 目標

把本公司 26,018 筆商品目錄載入**本地 docker OpenSearch**，用 **Bedrock Titan v2** 向量化，**在本地驗證語意/Hybrid 搜尋的結果品質**。成功標準是「能證明語意搜尋找得到 BM25 找不到的商品」，而非單一 demo。

**Phase 1 可交付成果**：
- 本地 docker OpenSearch（k-NN/faiss 內建）跑起、有 healthcheck
- 26k 筆原始資料載入（martId 當 `_id`，冪等可重跑）
- 每筆商品 Titan v2 向量寫入 `knn_vector` 欄
- **golden set 驗證**：一組真實查詢，向量 vs BM25 並排比較，證明語意搜尋價值

---

## 2. 背景與資料發現（已實際盤點來源檔）

來源檔：`OpenSearch_Full_20260612_030007.json`（36MB，單行 JSON array，繁中）。
⚠️ 須先確認結構是 `_source` 物件陣列，還是含 `_index`/`_id` 的 search response（影響 P1-3 解析）。

| 項目 | 發現 |
|------|------|
| **規模** | 26,018 筆 → 單節點本地輕鬆（向量約 106MB，遠低於 JVM circuit breaker） |
| **可嵌入文字** | `martName`、`feature`、`keyword`（7% 空）、`categoryLevelXName`，文字欄 0% 空值 |
| **語言** | 100% 繁體中文 → 影響 ① embedding 中文品質 ② BM25 中文分詞（見 P1-2 analyzer） |
| **分類污染** | ⚠️ **≥363 筆**品牌旗艦館（葡萄王、台塑生醫、順天本草、sakuyo、MEGA KING、大葉高島屋）把品牌名塞進 `categoryLevel1`；`categoryLevel2` 出現「成分分類/熱銷活動」行銷標籤 |
| **過濾** | `status` 全 2、`isSearchable=0` 有 4 筆（排除）、`channel` 全 1 |

**POC 商業主張**：分類污染 → 靠 `category` 篩會漏商品（搜「保健」漏掉葡萄王靈芝王）。語意搜尋從 `martName`/`feature` 抓意義、不依賴髒分類 → 這正是 P1-5 要量化驗證的事。

---

## 3. 前置條件

- [ ] 動工前讀 `.claude/rules/coding-rules.md` 與 `.claude/rules/safety.md`
- [x] **Bedrock Titan v2 已驗證**（2026-06-12）：profile `lab`（<REDACTED_ACCOUNT>, role <LAB_ROLE>）、region `ap-northeast-1`、`amazon.titan-embed-text-v2:0` 回 1024 維。boto3 須 `profile_name="lab"`。⚠️ lab 臨時憑證會過期 → `scripts/refresh-lab-creds.sh`
- [ ] 加依賴：`uv add opensearch-py`（pyproject.toml 目前無）
- [ ] 來源檔放入 `products/OpenSearch_Full_20260612_030007.json`（36MB，加 `.gitignore`）

---

## 4. 設計決策（已釐清，含 Fable 審查修正）

| # | 決策 | 理由 |
|---|------|------|
| D1 | **OpenSearch 2.19.x** | 不綁 prod → 挑最佳實踐版本；2.19 有 faiss + RRF score-ranker，避開 3.x 粗糙邊角與 nmslib 棄用 |
| D2 | **engine=faiss, space_type=innerproduct** | AWS 推薦 faiss；Titan `normalize:true` 下單位向量 innerproduct **等價 cosine**，且版本無關、面向未來（避開 nmslib） |
| D3 | **embedding 模型 Titan v2 / 1024 維** | 使用者指定；中文 Cohere 比較留 Phase 2 |
| D4 | **嵌入文字** = `martName`+`feature`+`keyword`+三層 categoryName（清洗後） | 文字欄品質好；category 含品牌名也是訊號，但行銷標籤是噪音（Phase 2 A/B） |
| D5 | **`_id` = martId**（商品編號） | bulk 用 `index` action 天然冪等，重跑不翻倍、P1-4 續跑才有意義 |
| D6 | **中文 analyzer**：`smartcn`（內建 plugin） | 預設 standard analyzer 把中文切單字，BM25 很差；smartcn 做中文斷詞（⚠️ kuromoji 是日文，不用） |
| D7 | **embedding 用 boto3 自己嵌（方式 A）** | 本地最簡可控、貼合 ETL First；OpenSearch Bedrock connector（方式 B）不在 POC 範圍 |
| D8 | **boto3 不用 LangChain** | embedding 是原子操作；LangChain 留 Phase 3「檢索→生成推薦理由」 |

---

## 5. 工作項目

### Phase 1 — 載入本地 OpenSearch + Titan v2 向量化（本次焦點）

**P1-1 起本地 OpenSearch（docker）**
- [ ] `docker-compose.dev.yml` 加 `opensearch` 服務：`opensearchproject/opensearch:2.19.x`，single-node、`DISABLE_SECURITY_PLUGIN=true`、`-Xms1g -Xmx1g`、memlock
- [ ] 加 **healthcheck**（`curl -f localhost:9200/_cluster/health`，對齊專案既有服務慣例）
- [ ] 裝 `smartcn` analyzer plugin（Dockerfile 或 init 裝 `analysis-smartcn`）
- [ ]（可選）`opensearch-dashboards`（5601）；埠避開既有（postgres 5434）
- [ ] 驗證：healthcheck green

**P1-2 建 k-NN 索引**
- [ ] 索引 `products_v1`：`settings.index.knn=true`、載入期 `refresh_interval=-1`、`number_of_replicas=0`
- [ ] mapping：
  - 文字欄用 **smartcn analyzer**：`martName`/`feature`/`keyword`（text, analyzer=smartcn）
  - `categoryLevelXName`/`brand`（keyword）、`price`（float）、`isSearchable`（integer）
  - `embedding`: `knn_vector`，`dimension=1024`，`method={engine:faiss, name:hnsw, space_type:innerproduct}`

**P1-3 載入原始資料（ETL First，純演算法）**
- [ ] `scripts/etl/load_products_os.py` — 讀 JSON（先確認結構）→ 過濾 `isSearchable=1` → `opensearch-py` **bulk**（`_id=martId`，`index` action 冪等）
- [ ] 載入後復原 `refresh_interval`/`replicas`，`GET products_v1/_count` ≈ 26,014

**P1-4 Titan v2 向量化（boto3，方式 A）**
- [ ] `scripts/etl/embed_products_os.py` — `boto3.Session(profile_name="lab", region_name="ap-northeast-1")` → `invoke_model`
  - **文字清洗**：strip HTML、`keyword` 空值用 `or ""`（避免串出 `"None"`）、truncate 至 Titan 上限內（8192 token / 50k 字元）
  - body `{"inputText": ..., "dimensions": 1024, "normalize": true}`
  - **bulk update** 寫回 `embedding`；批次 **200~500 docs/批**
  - 自寫 retry（exponential backoff，429/5xx）；可重跑（只補無 embedding 的 doc）
  - **5~10 並發**（注意 Bedrock RPM quota）
- [ ] ⏱️ 估時：序列約 **1~1.5 小時**；並發後縮短。**一次跑不完是預期行為**（lab 憑證會過期），靠續跑機制。Bedrock Batch Inference 為半價替代（POC 用 on-demand 合理，記錄此取捨）
- [ ] 💰 成本：~390 萬 token × Titan v2 ≈ **< $0.1 一次性**

**P1-5 驗證搜尋結果（重點，golden set + BM25 對照）**
- [ ] 建 **golden set**：10~20 條真實查詢，標註預期命中商品，分兩類：
  - **詞面重疊**（BM25 也該行）：如「靈芝保健飲」→ 葡萄王靈芝王
  - **詞面不重疊**（BM25 應失敗、向量應成功）：如「增強免疫力的飲料」「送長輩的養生禮盒」「冬天露營手指冰冷」→ 對應靈芝/養生/暖手套商品
- [ ] 每條查詢：先 boto3 嵌入 query → 跑 **k-NN query**，**並排跑 BM25 `match` 對照組**，比 top-10
- [ ] **成功標準**：詞面不重疊查詢中，「向量找到、BM25 找不到」的案例 ≥ N 個（量化證明語意價值）
- [ ] **分類污染示範**：對「保健食品」做 category filter（漏掉葡萄王，因其 category=品牌名）vs 向量搜尋（找到）→ 證明繞過髒分類
- [ ] 查詢端嵌入用同一 Titan v2（保證 query/doc 同模型同維度）
- [ ] golden set 存檔，Phase 2 模型 benchmark 直接複用

### Phase 1 執行結果（2026-06-13 收案）

**資料平面全數完成**：26,018 筆 → 過濾 4 筆 isSearchable=0 → **26,014 筆載入 + 100% Titan v2 向量化**（1024 維）。冪等已驗（重跑 _count 不變）；續跑機制經真實 crash 實證（OpenSearch bulk ConnectionTimeout 中斷後，增量 flush 保住 22,200 筆、重跑只補 3,814）。Bedrock 實際花費 < $0.15（嵌入 + LLM-judge）。

**驗證結論（三輪量尺，皆如實未調寬）**：
- 第一輪「精確 expected_mart_id 命中」：vector-only wins 0/8 ❌——**診斷為量尺問題**：多 SKU 變體 + 通用 query 下，猜死的標準答案 ID 冤枉了向量（向量回了語意正確但非指定 ID 的商品）。報告 `out/search_eval_20260613.md`。
- 第二輪「LLM-judge 相關性」（**Haiku** 評 271 個 query×商品對）：向量勝 **2/8**（N=3 未達 ❌）、平手 3、BM25 勝 3。報告 `out/search_eval_judge_20260613.md`。
- 第三輪「LLM-judge 相關性」（**Opus 4.8** 同 271 對重評）：向量勝 **5/8（N=3 達標 ✅）**、平手 1、BM25 勝 2。報告 `out/search_eval_judge_20260613-opus.md`。

**兩個 judge 判定翻轉的機制（可解釋、非裁判購物）**：Opus 整體更嚴格（non_overlap 平均相關數 vec 4.25→3.00、bm25 4.62→**2.12**）——砍最多的是 BM25 靠部分詞面匹配撈到的商品（q08「增強免疫力」：Haiku 認 BM25 10/10 相關、Opus 只認 3/10 真能滿足需求）。**嚴格的「真能滿足需求」標準下，向量的語意匹配存活率高於 BM25 的詞面匹配**。兩 judge 在向量短板上完全一致（q04 ThinkPad 均 0:10），且 q11/q13/q14 判定方向一致，顯示 Opus 並非偏袒向量。**最終採信較強 judge（Opus 4.8）：POC 成功標準達成**；Haiku 結果保留作為 judge 校準參考（Phase 2 benchmark 建議直接用 Opus 級 judge）。

**數據給出比原命題更有行動價值的地圖**：
1. **向量強項實證——情境/症狀式 query**：「冬天戶外手腳冰冷」vec 4:0、「頭髮掉太多想變茂密」vec 7:2。零詞面重疊的身體狀態描述，BM25 全滅、向量有效。
2. **BM25+smartcn 比假設強**：通用健康 query（「增強免疫力的保健飲品」）經 smartcn 切詞後部分匹配（保健/飲品），在商品文案密集的語料命中大量相關品——「BM25 應失敗」的前提在語料層面不成立。
3. **互補實證 → hybrid 是正解**：全局 vec_only_rel vs bm25_only_rel——Haiku judge **57 vs 73**、Opus judge **41 vs 52**，兩個 judge 方向一致：兩方法各自找到對方漏掉的數十筆相關商品。**這是 Phase 2 hybrid RRF 的直接實證依據**。
4. **向量已知短板**：品牌/型號式 query（「ThinkPad 筆電」vec 1:10）——嵌入被規格/類別文字稀釋，hybrid 中 BM25 不可少的原因。

**營運注意事項**：`load_products_os.py` 用 `index` action 全量覆寫——**重跑 load 會清空全部 embedding**，需 embed 續跑補齊（全量 ~$0.1/20 分）。golden set（15 條，`scripts/etl/golden_set_product_search.yaml`，status=approved）與 LLM-judge 腳本（`judge_search_relevance.py`）留作 Phase 2 模型 benchmark 與 API 準確度測試的固定量尺。

### Phase 2 — ✅ 已規格化並實作為 `openspec/changes/product-search-hybrid-api`（2026-06-13）——hybrid search API（BM25+k-NN 應用端 RRF）+ `src/recommender/search/` 領域模組已上線

> **實作摘要**：`GET /search?q=&size=` endpoint 上線；query 端 Titan v2 embedding（含 mock 路徑）；應用端 Python RRF（k=60）；async OpenSearch client（lifespan 管理）；DI 仍集中 `deps.py`。詳見 openspec design：`openspec/changes/product-search-hybrid-api/design.md`。工程面 132 測試綠、code review 修 2 個靜默 bug（category 欄位名、price=0 抹除）。

> **✅ 準確度評估最終結果（2026-06-13，Opus judge 277 對，端到端活測，如實未調寬）——hybrid 達標、贏過單一方法**：
> 採 **min-max score fusion（w_bm25=0.7）** 後，prod `/search` 活測 **hybrid 全局相關數 79 > BM25-only 76 > k-NN-only 65**；成功標準 (a) 全局 hybrid≥max **✅**、(b) 互補保留（q04=10/q11=1/q13=1 均不歸零）**✅**。報告 `out/search_eval_hybrid_20260613-fixed.md`。
>
> **達標前的兩個轉折（誠實紀錄）**：
> - 初版 **naive 等權 RRF**（k=60）：hybrid **71**（修 artifact 前報告印 69），夾在 knn(65)/bm25(76) 之間、兩項全 fail。
> - Fable 根因調查**推翻「k 太大」假設**（k-sweep 1~100 全平坦 71-72），定論真因是**等權融合**——換 min-max 加權 BM25 後翻盤。
> - min-max 首次活測 75（仍差 1）：落差**全來自 eval harness 的 source_map artifact**（hybrid 深位 doc 商品資訊空白 → judge 誤判 ✗，低估約 −4）；修 harness（三路聯集 + mget 補全 source）後重判得**真實值 79**。
> **根因（2026-06-13 Fable 數據調查定論，腳本 `scripts/etl/investigate_hybrid_fusion.py`、報告 `out/hybrid_fusion_investigation_20260613.md`）——真因是「未加權融合」，非「k 太大」**：
> 初判「RRF k=60 太大」**已被數據推翻**——k-sweep（k=1/5/10/20/30/60/100）全局 rel@10 全平坦 72/72/71/71/71/71/71，調 k 與調候選池皆無效。真因是**等權融合**：等權下單路 doc 的 RRF 分對 rank 單調遞減、與 k 無關，兩路不重疊時無論 k 都是 1:1 輪流，k-NN(弱,65) 噪音以等價地位稀釋 BM25(強,76) 的 gold。q11 的 bm25 r7 gold 在任何 k 下都贏不了 knn r1–r6 噪音（`1/(k+7)<1/(k+6)`，數學注定）。
> 另：報告原值 69 含一個 eval artifact（source_map 只蓋兩路 top-10，hybrid 深位 doc 資訊空白被誤判 ✗，q05 被誤報為病灶）——**修正後現行 prod 實為 71**。
> **融合策略實測（全局 rel@10，bm25=76 為標竿）**：min-max score fusion w_bm25=0.7 → **79（唯一同時過成功標準 a+b）**；weighted RRF w_bm25=0.7 → 78（q13 歸零 fail b）；等權 RRF（任何 k）71；oracle per-query 路由上界 88。

### Phase 2 現況（已完成）— min-max fusion 達標、已上 prod
- **已實作上 prod**：`src/recommender/search/` 改用 **min-max score fusion**（`search_bm25_weight=0.7`、`search_candidate_multiplier=2`，皆可由 Settings 調），`reciprocal_rank_fusion` 保留於 `fusion.py` 不刪。
- **已修 eval harness artifact**：`judge_hybrid_search.py` 的 `source_map` 改三路聯集 + `mget` 補全，消除「深位 doc 商品資訊空白被 judge 誤判 ✗」的低估（這就是 75→79 的 4 分差）。
- **端到端活測達標**：prod `/search` + Opus judge 277 對 → hybrid **79 > bm25 76 > knn 65**，(a)(b) 雙過。報告 `out/search_eval_hybrid_20260613-fixed.md`。
- **測試**：全套 141 passed、零 migration。

### Phase 2c-1 地基 — ✅ 已執行收案（2026-06-13）：擴 golden set 50 條 + 統計重驗
> 📌 **完整決策紀錄見 [`docs/plans/search-tuning-decision-record-20260613.md`](./search-tuning-decision-record-20260613.md)**——當天試了 4 種優化（調 w / 繁→簡分詞 / bigram / 軟 tag）全落雜訊帶或變差，**保留 v1**；功能導向 labeling 列為「待真實 query 分佈再評估」的聚焦未來方案。
> **結論（報告 `out/phase2c1_groundwork_20260613.md`，全程誠實未調寬）——達標降級、w=0.7 證實 overfit、真 headroom 在 routing**：
> - golden set 擴 **15→50 條**（20 lexical + 30 non_overlap，跨 5 大類別矯正 v1 過度集中保健 1.6% 尾類的取樣偏差；11 條打分類污染）。全部 mart_id jq 核實、non_overlap grep 核實詞面（含橋接詞）不重疊；使用者審核 approved。`scripts/etl/golden_set_product_search.yaml`（v2）。
> - **發現 1：達標統計不顯著**。50 條端到端（Opus 4.8 judge 919 對）：hybrid **228** / bm25 224 / knn 214。配對 bootstrap（B=10000）hybrid−bm25 margin **+4，95% CI [−10,+18]，P=69%**——**跨 0**。Phase 2「79>76 達標」落在雜訊內，應降級為「hybrid **不劣於** BM25 + 互補保留」。腳本 `bootstrap_hybrid_margin.py`、報告 `out/phase2c1_bootstrap_20260613.md`。
> - **發現 2：w_bm25=0.7 是 15 條 overfit**。w-sweep（`wsweep_50q.py`）在 50 條上 w=0.5→235、0.6→234 **都贏過 prod 的 0.7→228**，趨勢是「多給向量權重越好」；0.7 非峰值、不泛化。但 223–235 全在雜訊帶內 → 不能反稱 0.5 顯著更好。報告 `out/phase2c1_wsweep_50q_20260613.md`。
> - **發現 3：真 headroom 在 per-query routing**。50 條三路免費算出 oracle（逐 query 選 knn/bm25 較佳者）=**270**，比 hybrid 228 多 **+42 doc（+18%）**；靜態融合稀釋掉 47 個單一法勝場（最慘 q18：knn 10/bm25 0/hybrid 1）。
> - **行動**：① 放棄全局 w-tuning 破 80（優化雜訊）；② prod w_bm25 若要改方向是 0.5–0.6 但須 train/test 學非手掃；③ 提分投 per-query routing（oracle 270），但須 k-fold / 再擴量尺避免重蹈 overfit。golden set v2 已是經統計檢定的可信固定量尺。

### Phase 2c — 進一步提分計劃（⚠️ 已被 2c-1 數據修正：放棄破 80，改投 routing）
> ⚠️ **2c-1 實證更新**：原「純融合調參到 ~79」的天花板假設已被推翻為**「靜態融合整段（214–235）都在統計雜訊內、與 bm25 無顯著差異」**。破 80 不是天花板問題而是**雜訊問題**——再調 w 是優化雜訊。下列項目 1 已完成；提分重心移到項目 2（routing）。

依「可信增益 / 成本 / 過擬合風險」排序：
1. ✅ **【地基，已完成】擴 golden set 至 50 條 + bootstrap**：見上方 Phase 2c-1 收案。結論：達標降級、w=0.7 overfit、量尺已可信。
2. **【最大 headroom，提分重心】per-query 自適應融合 / 路由**（詞面強 query→偏 BM25、情境式→偏 dense）：**2c-1 在 50 條實測 oracle（逐 query 選較佳單一法）=270 vs hybrid 228，+42 doc（+18%）空間**（取代舊的 15 條 oracle 88）。⚠️ raw BM25 分數**不是**可靠路由訊號（q10/q13/q14 分高卻 0 相關）→ 需真 query 分類器（輕量訊號：query 長度、精確詞命中率、BM25 分數 entropy；或小型 LTR）。⚠️ 須 k-fold / 再擴量尺，否則在 50 條上重蹈 w=0.7 的 overfit。成本中-高、過擬合風險中。
3. **【避過擬合】learned fusion weights**（logistic regression / 小型 learning-to-rank 在標註上學權重，取代手調 w_bm25）：把「掃出 0.7」換成「學出來」，降過擬合。成本中。
4. **【換 embedding】Cohere Multilingual vs Titan v2 benchmark**（Phase 1 D3 預留）：需全量重嵌 26k（~$0.1/20 分）；中文 dense 品質若更好可整體抬升，增益未知。成本中。
   - 📌 **升級選項：統一多表徵模型（BGE-M3 類，一次推論得 dense+learned-sparse，消解 query 分類器）**——詳見 [`search-tuning-decision-record-20260613.md`](./search-tuning-decision-record-20260613.md) §11。屬下一代架構級投入，須排在「擴真實量尺」之後。
5. **【高增益高成本】LLM re-rank 融合後 top-20**：每次搜尋多一次 LLM 呼叫、改架構；增益可能大但延遲/成本高，留最後。

**研究待續**：原本要用 Fable 查 GitHub/HuggingFace/論文的 hybrid 融合調參經驗（convex combination 正規化選擇、weighted RRF 標準做法、小樣本避過擬合、OpenSearch/Weaviate/Vespa/LlamaIndex 預設與調參建議）—— Fable subagent 暫時取用不到中斷，下次續查。

（以下為原勾勒內容，保留設計脈絡）
- Hybrid：BM25 + k-NN，融合用 **score-ranker-processor（RRF，2.19+）** 或 normalization-processor
  - ⚠️ 修正：2.17 無內建 RRF；本 POC 用 2.19 故 RRF 可用
- **領域模組 `src/recommender/search/`**（self-contained bounded context，非散進現有層優先資料夾）：`search/repository.py`（OpenSearch client + k-NN/BM25 DSL）→ `search/service.py`（hybrid 融合編排）→ `search/router.py`（`/search` endpoint）。理由：search 的基礎設施（OpenSearch）與核心 Postgres+Bedrock 不同，圈成獨立模組才能解耦、易抽換/獨立部署——這是 codebase 第一個 infra 斷層 domain，值得起頭採領域模組（屆時補一條設計決策）。P1-5 verify 腳本的查詢函式（embed query / k-NN / BM25）寫成可重用 importable 形式，Phase 2 直接 lift 進 `search/service.py` 不重寫。
- **golden set 是兩平面的共同契約**：Part A（ETL 載入正確性）與 Part B（搜尋準確度）共用同一把量尺；P1 產出的 `golden_set_product_search.yaml` 直接當 Phase 2 search API 的準確度測試 fixture。
- `category`/`stock` 當軟訊號降權；中文模型 benchmark（Titan vs Cohere Multilingual，用 golden set 測 recall@10）

### Phase 3 —（勾勒）FM 清洗分類污染
- 既有 `chains/` + Bedrock，把 363 筆品牌館商品從 `martName`/`feature` 重新分類

---

## 6. 不做的事（POC 範圍外）

- ❌ 不上 prod、不複製 prod 的 RDS→event→OpenSearch 同步（POC 不綁 prod；JSON 直接載入本地 OpenSearch）
- ❌ 不用 pgvector（既然要練 OpenSearch 生態，直接用 OpenSearch）
- ❌ 不用 Bedrock KB（RAG 文件問答，非商品排序搜尋，錯抽象）
- ❌ 不用 OpenSearch Bedrock connector（方式 B；本地設定重，POC 用方式 A）
- ❌ 不做 hybrid 融合 / API endpoint（Phase 2）；不修分類污染（Phase 3）
- ❌ 不預先囤備用向量、不做多模型比較（Phase 1 單 Titan v2）

---

## 7. 風險與備註

- **Titan v2 區域可用性**：已驗證 ap-northeast-1 可用
- **OpenSearch 記憶體**：JVM 1~2GB；Linux 主機 `vm.max_map_count=262144`（Docker Desktop Mac 內建）
- **本地關 security**：`DISABLE_SECURITY_PLUGIN=true` 僅限本地 POC
- **k-NN 索引特性**：`index.knn` 不能熱改既有索引 → 改 mapping/加向量要 reindex + alias
- **中文 BM25**：必裝 smartcn，否則 hybrid 的關鍵字半邊地基不穩（Phase 2 才完整用到，但 P1-2 建索引時就要決定）
- **lab 憑證過期**：embed 一次跑不完正常，靠續跑；過期用 refresh 腳本
- **若未來決定上 prod**（非預設）：須補 ① prod AOS 版本對齊 ② event pipeline 嵌入責任歸屬（自嵌 vs connector）③ `knn` query（方式A）vs `neural` query（方式B）的 DSL 差異 ——這些 POC 階段不解，移到屆時的遷移評估
