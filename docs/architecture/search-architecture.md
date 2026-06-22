# 搜尋子系統架構（Hybrid Product Search）

> 本文是 `search_engine` 模組（`src/search_engine/`）的**完整、最新**架構說明，是搜尋子系統的權威文件；全域 [`architecture.md`](./architecture.md) §5.8 為高層摘要並指回本文。上線採 **min-max score fusion**（融合）+ **Cohere Embed v4 / 1536 維**（向量化，索引 `products_v5_cohere`）。深層調優過程、失敗模式與決策依據見 [`../plans/search-tuning-decision-record-20260613.md`](../plans/search-tuning-decision-record-20260613.md)。

## 1. 定位與範疇

把本公司 26k 商品做**語意 / hybrid 搜尋**的本地 POC。核心主張：純詞面（BM25）搜不到「冬天手腳冰冷 → 暖手寶」這種症狀↔功效查詢，需要語意向量補足；但純向量又有極性盲（冷↔熱）與詞面精確不足（型號/品牌），所以採 **hybrid = BM25 + k-NN 融合**。

- **不上 prod**：本地 docker OpenSearch + Bedrock API 驗證搜尋品質。
- **獨立模組**：`search_engine` 是與 `recommender` 平行的 top-level 模組（儲存基礎設施是 OpenSearch + Cohere embedding，與核心 PostgreSQL+Bedrock 不同），同 app mount、沿用 recommender.config（理由見 architecture.md §5.8）。

## 2. 高層架構：兩條腿 + 融合

```
query → 設 bm25_weight → 兩條腿並發 → min-max 融合 → DTO
                          ├─ 向量腿(k-NN, Cohere v4 dense)
                          └─ 詞面腿(BM25, smartcn 分詞)
```

- **詞面腿（BM25）**：query 經 smartcn 中文分詞 → `multi_match` 打 `martName/feature/keyword`。擅長型號/品牌/精確商品名；弱於症狀描述、且繁體 smartcn 退化成逐字切會有「腳」單字碰撞。
- **向量腿（k-NN）**：query 經 **Cohere Embed v4 嵌成 1536 維**（`input_type=search_query`、L2 正規化）→ faiss/hnsw/innerproduct k-NN。擅長語意/症狀；中文語意深度優於前代 Titan v2（真實檢索實測大幅緩解「手腳冰冷→製冷」極性盲），但極性與長尾仍有殘餘尾巴。
- **融合**：應用端 **min-max score fusion**——每路 raw `_score` 各自 per-query min-max 正規化後加權相加：`fused = w_knn·norm(knn) + w_bm25·norm(bm25)`，`w_knn = 1 - w_bm25`。**現行 prod `w_bm25=0.2`**（換 Cohere v4 後從 Titan 時代的 0.7 重調——向量腿變乾淨，最優權重往向量側移）。

> ⚠️ **下文「為何 min-max」段落出現的 `w_bm25=0.7` 是 Titan 時代的歷史分析**（解釋 RRF→min-max 的動機），非現行值。換模型 = 換最優融合權重：Cohere v4 的 4 條旗艦 query w-sweep 顯示症狀 query 在 `w_bm25≤0.2` 才正確、關鍵字 query 在任何 w 皆穩，故 prod 定 0.2（見 `config.search_bm25_weight` 註解）。

### 為何 min-max 而非 RRF

本專案 **Phase 2 初版用的就是 RRF**（等權 `Σ 1/(k+rank)`），後來才換成加權 min-max（commit `828507f`：「等權 RRF 換成加權 min-max，hybrid 79 達標」）。換的理由是 RRF 兩個結構性限制：

| 面向 | RRF | min-max | 對本專案的影響 |
|------|-----|---------|----------------|
| 融合依據 | 只看 **rank**（第幾名）| 保留 **raw _score** 強度 | 型號精確查詢（ThinkPad）BM25 對第一名是「45 分海放 8 分」的碾壓，RRF 只當「第 1 名 vs 第 2 名」一個名次差，表達不出強信心 |
| 權重 | 原版**等權**（兩腿一視同仁）| 內建 `w_bm25` 可調 | 等權 RRF 用較弱的腿稀釋較強的腿，hybrid 融合分穩不過單獨 BM25；調 `w_bm25=0.7` 讓 BM25 主導後才在 15 條 golden set 上 `79 > bm25 76 > knn 65` |
| prod／離線一致 | — | 對齊 `investigate_hybrid_fusion.py` 的 `minmax_fusion` | 線上結果 = 離線調參量到的 79（見 `fusion.py` docstring）|

**誠實補述（避免把 min-max 說成銀彈）**：① 真正的槓桿是「**加權**」而非 min-max 本身——weighted RRF（`Σ wᵢ/(k+rank)`）一樣能加權；選 min-max 一半是為了對齊離線調參harness。② 50 條 golden set + bootstrap 重驗顯示 hybrid−bm25 的 margin **落雜訊帶內（CI 跨 0）**、`w_bm25=0.7` 有 overfit（見 [`../plans/search-tuning-decision-record-20260613.md`](../plans/search-tuning-decision-record-20260613.md)）——所以正確結論是「min-max 讓我們**能加權**、加權清掉 15 條門檻，但優勢脆弱」。換 Cohere v4 後**所有 query 類型（症狀／關鍵字／品牌／型號）的最佳融合權重都收斂到同一個低值 `w_bm25=0.2`**，沒有「不同類型要不同權重」的 headroom 可榨；真正的提升來自**換模型 + 固定低權重**，而非融合公式或動態調權重。③ min-max 的代價：對離群高分敏感（一個極高分會把其他 doc 壓近 0）、需 per-query 正規化邊界處理（`fusion.py:_normalize` 的 `hi==lo→1.0`）。正因此 `reciprocal_rank_fusion` 仍保留作免調參對照基準，未刪除。

## 3. 模組結構（`src/search_engine/`）

| 檔案 | 職責 |
|------|------|
| `router.py` | `GET /search`：驗 `q`(必填)/`size`(1–100)/`bm25_weight`(可選 0–1 手動覆寫)；注入 `SearchServiceDep`，不碰 client、不拋 HTTPException |
| `service.py` | `SearchService.search(query, size, bm25_weight)` 編排全鏈路：權重解析 → embed → msearch → 融合 → DTO。mock 判斷在此 |
| `repository.py` | 純 I/O：`build_knn_body`/`build_bm25_body` 建 DSL、`hybrid_msearch` 一次 msearch 並發兩路 |
| `fusion.py` | 純函式融合：`min_max_score_fusion`（上線用）+ `reciprocal_rank_fusion`（保留，僅單元測試用）。零 I/O、可測 |
| `embeddings.py` | `@lru_cache get_bedrock_embeddings(...)` 回 cached Cohere v4 query embedder（boto3，`input_type=search_query`、`output_dimension=1536`、L2 正規化、`asyncio.to_thread` 非阻塞）；`MOCK_QUERY_VECTOR`（1536 維固定單位向量）|
| `client.py` | `@lru_cache get_opensearch_client()` 回 `AsyncOpenSearch` 單例；`close_opensearch_client()` 供 lifespan shutdown |
| `schemas.py` | `SearchResultItem` / `SearchResponse` DTO；含觀察欄位 `applied_bm25_weight` / `route_label`（現只會是 "manual" 或 None）|

> 模組內仍遵循 router→service→repository 三層職責；DI wiring 只在 `deps.py`；async OpenSearch client 生命週期接 `main.py` lifespan。

## 4. 資料平面（索引）

| 索引 | analyzer（BM25 腿）| embedding（向量腿）| 狀態 |
|------|------|------|------|
| **products_v5_cohere** | smartcn（繁體逐字切）| **Cohere Embed v4，1536 維** | ✅ **prod 預設**（中文語意檢索大幅優於 Titan，真實檢索實測壓倒）|
| products_v1 | smartcn | Titan v2，1024，含 feature | 前 prod（Titan baseline，golden set hybrid 228）|
| products_v2 | smartcn + 繁→簡(stconvert) | 同 v1 複製 | 實驗（分詞改→整體 217，更差）|
| products_v3 | t2s 詞 + cjk_bigram 多欄位 | 同 v1 複製 | 實驗（212，最差）|
| products_v4_nofeat | smartcn | Titan v2，去 feature | 實驗（225≈v1 但修掉向量污染）|

**嵌入不變量**：query 與 doc 必須同模型、同維度、同 normalize（**Cohere Embed v4 / `output_dimension=1536` / L2 正規化 + innerproduct**）。Cohere float embedding 非單位長，故 doc 端（`embed_products_os.py`）與 query 端（`embeddings.py`）**兩端都手動 L2 正規化**，innerproduct 才等價 cosine；任一端不正規化就是兩向量活在不同空間、k-NN 分數靜默全錯。另：doc 端 `input_type=search_document`、query 端 `input_type=search_query`（Cohere 不對稱編碼）。

## 5. 已知失敗模式與設計決策（詳見決策紀錄）

> 註：下表「失敗模式」多在 **Titan v2 時代**觀察到。換 **Cohere Embed v4**（即 prod `products_v5_cohere`，注意此「v4」是 Cohere 模型版本，與下文 Titan 去 feature 的實驗索引 `products_v4_nofeat` 不同）後，A/B 真實檢索實測已大幅緩解。

| 失敗模式 | 例（Titan 時代）| 根因 | 現況決策（Cohere v4 後）|
|---|---|---|---|
| **A 極性盲** | 手腳冰冷→製冷風扇 | dense embedding 按主題聚類、不分方向（NevIR：所有 dense ≈ 隨機）| **Cohere v4 中文語意深度足夠，真實檢索實測已大幅緩解**（手腳冰冷→暖手套，非製冷）；殘餘尾巴接受。註：極性是 dense 家族通病，但「中文品質不足」會放大它，換好模型即見效 |
| **B feature 污染** | 久坐肩頸痠痛→藍牙耳機 | feature 行銷套話（久戴舒適/人體工學）污染向量 | **Cohere v4 較不受套話污染**（實測肩頸痠痛→按摩披肩/頸肩熱敷，非耳機）；故 v5 嵌入文字仍含 feature。Titan 時代的去 feature 實驗（`products_v4_nofeat`）已不需要 |
| **C CJK 分詞** | 手腳冰冷→三腳架 | smartcn 繁體逐字切，「腳」碰撞（**BM25 腿問題，與向量模型無關**）| 試繁→簡/bigram 皆更差 → BM25 腿保留 smartcn；Cohere 向量腿乾淨後融合更穩，固定低 bm25 權重繞過 |

**未採用**：query 判型路由**已移除**（換 Cohere v4 後所有 query 類型的最佳權重都收斂到同一個低值 `w_bm25=0.2`，「不同類型需不同權重」的前提消失，路由變多餘）；cross-encoder/LLM rerank 不上（成本高、Cohere v4 已大幅降低需求、ROI 差）。

## 6. 完整資料流（`GET /search`）

```
GET /search?q=&size=10&bm25_weight=（可選）
 ↓ [1] router.py 驗參數
 ↓ [2] service._resolve_bm25_weight：手動 ?bm25_weight= > 固定 0.2
 ↓ [3] service._embed_query：mock→MOCK_QUERY_VECTOR；真→Cohere v4 aembed_query（search_query, L2 正規化）
 ↓ [4] repository.hybrid_msearch(vector, query, candidate_k=2×size)
 ↓     一次 msearch 並發：k-NN(faiss/innerproduct) + BM25(smartcn multi_match)
 ↓ [5] fusion.min_max_score_fusion(knn_scored, bm25_scored, w_bm25, w_knn)
 ↓ [6] top-size → _id join _source → SearchResultItem DTO
 → SearchResponse(query, results, applied_bm25_weight, route_label)
   查無結果回 results=[]、HTTP 200
```

## 7. 架構圖

```
                          GET /search?q=…&size=…&bm25_weight=(opt)
                                         │
                                ┌────────▼────────┐
                                │   router.py     │  驗 q / size / bm25_weight
                                └────────┬────────┘
                                         │
                          ┌──────────────▼───────────────┐
                          │        service.py 編排        │
                          │  _resolve_bm25_weight 優先序： │
                          │     手動 > 固定 0.2           │
                          └───────────────┬──────────────┘
                                          │ w_bm25 決定
                                          ▼
                          ┌───────── 兩條腿並發 (msearch 一次 round-trip) ─────────┐
                          │                                                      │
                 ┌────────▼─────────┐                              ┌─────────────▼────────┐
                 │  向量腿 k-NN      │                              │  詞面腿 BM25          │
                 │  Cohere v4 embed  │                              │  smartcn 中文分詞     │
                 │  1536/search_query│                              │  multi_match          │
                 │  faiss/innerproduct                             │  martName/feature/kw  │
                 │  candidate_k=2×size                             │  candidate_k=2×size    │
                 │  ✓語意/症狀       │                              │  ✓型號/品牌/精確       │
                 │  ~極性(v4 大幅緩解)│                             │  ✗繁體逐字切「腳」碰撞 │
                 └────────┬─────────┘                              └─────────────┬────────┘
                          │ raw _score                                          │ raw _score
                          └──────────────────────┬───────────────────────────────┘
                                                 ▼
                              ┌──────────────────────────────────────┐
                              │  fusion.min_max_score_fusion          │
                              │  per-query min-max 正規化後加權相加：  │
                              │  w_knn·norm(knn) + w_bm25·norm(bm25)   │
                              └───────────────────┬──────────────────┘
                                                  ▼
                                top-size → _id join _source → DTO
                                                  ▼
                       SearchResponse(query, results[], applied_bm25_weight, route_label)

   ┌─ 資料平面（索引）──────────────────────────────────────────────────────────┐
   │  products_v5_cohere(prod) smartcn    + Cohere v4 1536       中文語意大幅優 ✅ │
   │  products_v1        smartcn          + Titan v2 1024(含feat) 前 prod(228)     │
   │  products_v2        smartcn+繁→簡     + 同v1向量             217（更差）       │
   │  products_v3        t2s詞+cjk_bigram  + 同v1向量             212（最差）       │
   │  products_v4_nofeat smartcn          + Titan(去feature)     225（修向量污染） │
   │  不變量：query/doc 同模型同維度同 normalize（Cohere v4/1536/L2/innerproduct）│
   └────────────────────────────────────────────────────────────────────────────┘

   失敗模式（詳見決策紀錄）：A 極性盲(接受) · B feature污染(v4去feature 已修) · C CJK分詞(保留v1)
```
