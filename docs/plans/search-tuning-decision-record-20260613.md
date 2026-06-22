# 搜尋調優決策紀錄（2026-06-13）

> **狀態**：✅ 收案，停在驗證過的 **products_v1**（現 prod）。
> **一句話**：當天試了 4 種優化（調 w、繁→簡分詞、bigram、軟 tag），**全部落在 golden set 雜訊帶（±14-18）內、bigram 甚至明確變差**；結論是搜尋已逼近「50 條量尺能分辨的天花板」，**保留 v1**，把「意圖重排（A）」標記為**待真實 query 分佈數據再評估 ROI**。

---

## 1. 背景：為何啟動這趟調查

- Phase 2 宣告 hybrid「79 > 76 達標」（15 條 golden set）。兩個疑慮未解：① w_bm25=0.7 在同一份 15 條掃出又評估（拿尺量自己）② 樣本太小。
- 同時，使用者實測發現 query「冬天手腳冰冷」回傳「三腳架/磨腳皮機」，明顯荒謬。
- 這兩件事觸發：先擴量尺做統計重驗（Phase 2c-1 地基），再順著「手腳冰冷」一路追根因。

## 2. 量尺（所有結論的依據）

- **golden set v2**：15→**50 條**（20 lexical + 30 non_overlap，跨 5 大類別矯正 v1 過度集中保健的取樣偏差；11 條打分類污染）。`scripts/etl/golden_set_product_search.yaml`（status=approved）。
- **評估**：app `/search`（真 min-max 融合）+ 直打 OpenSearch knn/bm25，Opus 4.8 judge 每個 query×doc 對，算全局 rel@10。
- **統計**：配對 bootstrap（B=10000，`scripts/etl/bootstrap_hybrid_margin.py`）。**關鍵事實：50 條上 rel@10 的雜訊帶約 ±14-18 個 doc**——任何「贏 10 幾分」都可能是雜訊。

## 3. 四個發現（每個都是「直覺 → 數據打臉」）

| # | 假設 | 做法 | 數據 | 結論 |
|---|------|------|------|------|
| **1** | 達標 79>76 是真進步 | 50 條重驗 + bootstrap | hybrid **228** vs bm25 **224**；margin +4，95% CI **[−10,+18]**，P(hybrid>bm25)=69% | **不顯著**。達標應降級為「hybrid 不劣於 BM25 + 互補保留」 |
| **2** | 降 w_bm25 修手腳冰冷 | 0.7→0.4 端到端 | 手腳冰冷部分變好，但「ThinkPad 筆電」觸控筆擠掉真筆電 | **翹翹板**：救情境傷品牌。w-sweep 全段 223–235 在雜訊內 |
| **3** | smartcn 繁體逐字切是根因，繁→簡修分詞 | 裝 stconvert、建 products_v2、reindex | hybrid 217 / bm25 204；v1-vs-v2 hybrid **−11**，CI **[−38,+21]**，P=77%；**lexical +9 / non_overlap −20** | **重分配、整體 wash 偏負**。逐字切的「高召回」其實利多數 query |
| **4** | bigram 補召回拿回兩全 | 建 products_v3（t2s 詞 + cjk_bigram 多欄位） | hybrid **212** / knn 214 / bm25 201；**hybrid < knn**；q04/q11/q13 全歸零 | **最差**。bigram「筆電」把配件灌上來淹掉 ThinkPad（precision 崩） |

**三版總分對照（同一把 50 條尺、同 Opus 4.8 judge）**：

| 索引 | analyzer | hybrid | knn | bm25 | 判定 |
|------|----------|:---:|:---:|:---:|------|
| **products_v1**（現 prod）| smartcn（繁體逐字切）| **228** | 214 | 224 | ✅ 最佳 |
| products_v2 | smartcn + 繁→簡(t2s) | 217 | 212 | 204 | wash 偏負 |
| products_v3 | t2s 詞 + cjk_bigram 多欄位 | 212 | 214 | 201 | ❌ 最差 |

> **單調下降 228→217→212**：每次「修分詞」都讓整體更差。我們為了修一個**罕見可見病態**（手腳冰冷），做了一連串全域改動，越修越差。

## 4. 為何保留 v1（核心結論）

- **v1 在 golden set 上是三版最佳**，且是 Phase 2 已上 prod 的驗證狀態。
- 「逐字切」看似爛，但它的**高召回**在多數真實 query 上是優點（廣撒網撈到同類相關品）；「手腳冰冷→三腳架」只是高召回的罕見副作用。
- **紀律**：不上線未量測、或量測後變差的改動。v2/v3 量測後變差 → 不上線。

## 5. 「手腳冰冷」根因與正確修法

- **根因兩層**：① BM25 端——smartcn 繁體把「手腳冰冷」切成單字 `手/腳/冰冷`，「腳」碰撞「三腳架/磨腳皮」（已用 `_analyze` 證實）。② 向量端——embedding 把「冰冷」聯想到「製冷/冰感」降溫品（**意圖極性混淆**，分詞/bigram/調 w 都救不了）。
- **正確修法是「外科手術」而非「全域開刀」**：全域改分詞動到所有 query（已證明傷大於利）；只有**針對意圖極性的局部重排**才不傷無辜。

## 6. A / C 的設計與取捨（供未來動工參考）

完整解是一條 pipeline，兩層：
```
query →①意圖分類 ──────────────┐
                               ├→ ③重排(同意圖 boost / 對立意圖 penalty)   ← A
候選生成(分詞/bigram) →②商品意圖tag ─┘
   ↑ C
```

- **C（候選生成，bigram 多欄位）**：已試 = **失敗**（v3 整體最差，precision 崩）。candidate generation 的全域改動在此量尺上得不償失。
- **A（意圖 tag 重排）**：POC 已驗證**硬訊號有效**——在 v2 候選上，意圖 tag 重排把「發熱手套」從 #5 拉到 #2、把「製冷風扇」從 #2 壓到 #4。但：
  - **軟訊號無效**：把 tag 塞進 embedding 文字太弱（暖手品僅 +0.003~0.017，製冷風扇仍 #1）。必須用**硬訊號**（filter/boost）。
  - **A 依賴候選池**：在 v1（最佳整體）上，hybrid 候選靠 k-NN 那條腿其實**有**暖手品（v1 knn 診斷撈得到發熱手套/暖手寶），是 BM25 腳噪音在融合時壓過它們 → **A 或許能直接在 v1 hybrid 候選上做意圖重排，不必動分詞**。
  - **A 是大工程**：受控意圖詞表（~40-60 tag + 對立配對）→ LLM 標 26k（一次性、Haiku、結構化輸出、抽樣 QA）→ query 意圖分類器（輕量可快取）→ 重排層 → golden set 量測。
- **labeling 性質**：A 的標註不是傳統人工標註，是「人定受控詞表 + LLM 標 26k + 人抽查 QA」的半自動。

## 7. 決策

1. **停在 products_v1**（prod 不動）。v2/v3 索引保留於本地 OpenSearch 當實驗資產，不上線。
2. **A（意圖重排）標記為「待真實 query 分佈數據再評估 ROI」**。觸發 A 的條件：
   - 取得真實搜尋 log，量出「意圖極性混淆」類 query（手腳冰冷型）的實際佔比。
   - 若佔比夠高（值得為它建 pipeline），才啟動 A，且**直接在 v1 hybrid 候選上做重排**（不重蹈 v2/v3 全域分詞覆轍）。
   - 心理準備：A 的增益可能仍是雜訊級，需更大量尺才驗得出。
3. **真正解鎖下一輪的地基 = 擴真實量尺**：拿一週真實 query log、擴 golden set 到數百條真實 query。沒有這個，所有後續優化都「量不出真假」。

## 8. 留下的資產（路徑）

| 類型 | 路徑 |
|------|------|
| 50 條 golden set（量尺）| `scripts/etl/golden_set_product_search.yaml` |
| bootstrap 顯著性工具 | `scripts/etl/bootstrap_hybrid_margin.py` |
| w-sweep 工具 | `scripts/etl/wsweep_50q.py` |
| 地基決策報告 | `out/phase2c1_groundwork_20260613.md` |
| v1/v2/v3 評估報告 | `out/search_eval_hybrid_50q_20260613.md`、`out/search_eval_hybrid_v2.md`、`out/search_eval_hybrid_v3.md` |
| bootstrap / w-sweep 報告 | `out/phase2c1_bootstrap_20260613.md`、`out/phase2c1_wsweep_50q_20260613.md` |
| 搜尋測試 UI | `ui/search.html`（純 HTML/JS）|
| 本決策紀錄 | `docs/plans/search-tuning-decision-record-20260613.md` |

## 9. 動到 / 未動的東西（交接用）

- **prod search 已還原 v1 驗證基線**：`src/recommender/search/repository.py` 的 BM25 已回原狀（`["martName","feature","keyword"]`）；prod 索引仍 `products_v1`、`search_bm25_weight=0.7`。
- **保留的無害改動**：`main.py` 加 CORS（給 ui/search.html）；`verify_search_os.py` 的 `OPENSEARCH_INDEX`/`BM25_FIELDS` 改 env 可覆寫（預設＝原行為）；`docker/opensearch/Dockerfile` 裝了 `analysis-stconvert`（目前未用，留著供 A 評估）。
- **本地 OpenSearch**：products_v1（prod）+ products_v2/v3（實驗，可隨時刪）。
- **測試**：141 passed、零 migration。

---

## 10.（未來方案）功能導向 labeling 執行計畫——聚焦、先 slice 後放大

> 這是「A」的聚焦低成本版，由使用者的「功能導向」想法細化而成。**目標是修好「症狀型 query」的可見品質（如手腳冰冷不要回三腳架/製冷品），不是拉高整體 golden 分數**（已證明那會落在雜訊裡）。動工前須認清這個定位。

### 10.1 核心設計
- **功能 tag = 商品「用來做什麼」**（製冷風扇=降溫、暖手寶=保暖、葉黃素=護眼）。它在語意搜尋最弱的鴻溝上架橋：使用者打**症狀/需求**（手腳冰冷）、商品寫**功能**（保暖），兩者語意近但詞面/向量距離遠。
- **威力集中在「有對立極性」的功能軸**（保暖↔降溫、增重↔減重、保濕↔控油…）——大部分增益來自「罰對立」。無對立的功能（保護、充電）退化成弱 boost，不是重點。
- **重排機制（硬訊號，非塞 embedding）**：query 判功能 → 媒合商品 boost、對立商品 penalty。已 POC 驗證（發熱手套 #5→#2、製冷風扇 #2→#4）。⚠️ 軟訊號（tag 塞進 embedding 文字）已證實太弱，不要走。

### 10.2 labeling pipeline（信心路由 / human-in-the-loop）
- **不要用 LLM 自報信心 %** 當門檻——calibration 差、會自信地錯，且 Bedrock 不給 logprobs。
- **用 self-consistency（多次採樣一致性）當不確定性訊號**：同商品用 temperature>0 標 **3-5 次** →
  - 全部一致 → **自動接受**。
  - 出現 ≥2 種 tag 或落到「其他」→ **丟人工複核**。
- 受控功能詞表 + 結構化輸出（tag 限白名單）。成本：Haiku，全量 26k×5 次仍很便宜（一次性）。
- 這把人工量壓到只剩「低一致性」那一小撮，符合使用者的減量目標。

### 10.3 執行順序（紀律：先 slice 後放大，別重蹈「先做大再量」覆轍）
1. **先只標一條極性軸的 slice**：例如「風扇．電暖器」桶（製冷 vs 暖器，數百筆），標 保暖/降溫。
2. **建 query 功能分類器**（輕量 LLM，熱門 query 可快取）+ 重排層（boost/penalty）。
3. **在一小批真實「症狀型」query 上量**：確認目標類修好、且**沒誤傷其他類**（用 golden set 的非極性 query 當對照組）。
4. **放大 gate**：唯有 slice 驗證有效，才擴到全 26k + 完整功能詞表（多條極性軸）。

### 10.4 不變的前提與風險
- **整體 golden 分數不會明顯跳**（極性混淆 query 在 50 條尺裡僅 1-2 條）；這是可見品質/品牌觀感的投資，不是平均指標的工程勝利。
- **要驗「整體是否變好」仍需擴真實量尺**（第 7 節）——沒有它，只能宣稱「修好特定類」，不能宣稱「整體更好」。
- 風險：query 功能分類錯 → 重排錯（可能比不做更糟）；對立軸定義不當 → 誤罰合理跨功能商品。故第 3 步的「對照組不誤傷」是放大前的硬 gate。

### 10.5 已執行的 POC 實證（2026-06-13，「Do it」執行結果）

實際跑了「風扇．電暖器」slice（279 筆）的端到端 POC，結論如下——**機制正確、但逃不出候選池矛盾**：

- **labeling pipeline ✅ 驗證可行**：self-consistency（3 輪多數決）標 279 筆 → **99% 三輪一致自動接受、僅 2 筆待人工**。證實使用者的「高信心自動/低信心人工」可行，且**不確定性訊號要用「多輪一致性」不是「LLM 自報 %」**。產出 `out/slice_fan_heater_tags.json`（保暖 63/降溫 207/皆非 9）。
- **重排機制 ✅ 在候選池有 slice 商品時生效**：
  - v2「手腳冰冷」：暖手寶 #6→**#2**、製冷風扇被罰出 top-6。✅
  - 「夏天消暑」(v1)：DYSON 涼風扇被 boost 進前列。✅
- **❌ 但在 v1（整體最佳索引）上對「手腳冰冷」完全無效**：v1 top-30 候選全是三腳架/磨腳皮/杯子，**無任何 slice 商品**（暖手寶被 BM25 腳噪音擠出候選池）→ 重排無對象。**「候選池是閘門」經同一套 tag 在 v1/v2 結果相反而釘死**。
- **⚠️ 兩個新風險源實測浮現**：① query 分類器會錯（「氣炸鍋」被判成「降溫」，此次僥倖無傷因候選無 slice 商品）；② slice 覆蓋不全（發熱手套在別的 cat2、不在此 slice、未被 boost）。
- **根本矛盾（第 5 次驗證）**：修好「手腳冰冷」需要 v2/v3 的候選生成把暖手品撈進池，但 v2/v3 降整體分數。**功能 labeling 解決不了這個張力**——它是站在候選生成之上的層，候選不對它就無能為力。
- **詞表設計教訓——標籤本來就該是多標籤（multi-label），單標籤是本 POC 的過度簡化**：本次 slice 用單標籤（保暖 XOR 降溫 XOR 皆非）是錯誤的預設限制。2 筆「三輪搖擺」（DIKE 冷暖溫控扇、SONGEN 四季冷暖氣機）其實是被「只准選一個」逼出來的——它們是真實的雙功能商品，multi-label 下本該自然標成 `[保暖,降溫]`、不會搖擺。**正式做法：標籤一律 multi-label，一個商品可掛多個功能 tag；重排時「query 功能 ∈ 商品 tag 集合」就 boost、「對立功能 ∈ 商品 tag 集合且 query 功能不在」才 penalty**。雙功能商品（同時含保暖+降溫）對兩種 query 皆 boost、皆不罰，自然正確。本次已先把這 2 筆標 `冷暖兩用` 過渡（`out/slice_fan_heater_tags.json`）。
- **結論不變**：停在 v1。功能 labeling 若要做，須連同候選生成改動一起評估，且**整體增益仍需真實量尺才驗得出**——回到第 7 節的地基。

---

## 11.（未來升級選項）統一多表徵模型（BGE-M3 類）— Phase 2c「換 embedding」的升級版

> 由使用者提問細化：「有沒有一個模型，一次推論同時給『向量化』和『關鍵字 vs 語意』兩種結果？」答案是有——**多表徵 / 統一嵌入模型**，代表作 **BGE-M3**。這是 Phase 2c 原「換 embedding（Cohere vs Titan benchmark）」那條的升級方向：不只換 dense 模型，是換成「dense + learned-sparse」統一模型。

### 11.1 核心：一個模型、一次推論、多種表徵
BGE-M3 同一次 forward pass 同時輸出：
1. **Dense 向量**（語意）— 對應現在 Titan 做的事。
2. **Sparse / 詞彙向量**（learned lexical，帶詞權重）— 對應「關鍵字面」，但是**學出來的**，比 smartcn 原始 BM25 聰明。
3. ColBERT 多向量（可選，給重排用）。

### 11.2 為什麼比「Titan dense + smartcn BM25 + 脆弱分類器」優雅
- **消解掉 query 分類器**：不需「先判關鍵字/語意、再選權重」（本紀錄第 6、10 節證實該分類器二分法太粗、且多屬過度工程）。改成「同時拿語意表徵 + 關鍵字表徵，融合時每筆結果自己決定靠哪邊」——「關鍵字 vs 語意」變成**融合的隱含結果，不是會判錯的前置決策**。
- **learned-sparse 有望解掉今天的 CJK 痛點**：它學「詞的重要性」而非字面切分，可能繞過 smartcn 繁體逐字切的「腳碰撞」、並處理同義落差（掃地↔掃拖）——這些是 v2/v3 想修卻得不償失的問題。
- 若仍想要**明確的「X% 關鍵字」標籤**：在 dense 向量上接一個極小線性分類頭（同一次推論 → 向量 + 分類，近零成本），但分類頭要標註資料訓練。

### 11.3 誠實代價（屬「下一代架構」級投入，非快速調整）
1. **換模型**：Titan(僅 dense) + smartcn → BGE-M3（或 Cohere / SPLADE 類）。
2. **全量重嵌 + 改索引**：26k 重算 dense+sparse；OpenSearch 建 sparse 欄位（neural sparse / rank_features）+ 融合改寫。
3. **自架推論**：BGE-M3 開源、不在 Bedrock，需自架 GPU/CPU 推論服務（失去 Titan 的 managed 便利 + 引入新運維）。
4. **繁中品質未知**：multilingual learned-sparse 在繁體電商的實際表現，**沒測過不能假設更好**。
5. **不保證修極性**：dense 面可能仍「主題≠極性」（手腳冰冷↔製冷，本紀錄反覆驗證的 embedding 物理限制）；learned-sparse + 更好融合也許幫一點，須測。

### 11.4 定位與順序
- 這是 Phase 2c「換 embedding」那條的**具體升級候選**，也是「下一代檢索架構」實驗。
- **必須排在第 7 節「擴真實量尺」之後**：在 50 條 golden set（差異全落雜訊帶）上無法分辨 BGE-M3 是真更好還是換湯不換藥。沒有真實量尺，這個遷移的 ROI 驗不出來。
- 觸發條件：① 已有真實 query log 量尺 ② 確認現有 hybrid 在真實流量上確有不足（而非雜訊）→ 才值得評估這個遷移成本。
