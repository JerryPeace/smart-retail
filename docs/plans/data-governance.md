# Phase 1.5: 資料治理 / ETL

> ✅ **狀態:已完成,但 scope 與原計畫不同。詳見 [§9 實際產出 (Outcome)](#9-實際產出-outcome)**
>
> 接續 Phase 0 (scaffolding) + Phase 1 (Bedrock 整合) 之後的下一階段。
> 完整架構背景請看 [architecture.md](../architecture.md)。

## 1. 目標

把 `DatasetService.prepare()` 從目前的 stub 變成真實的 ETL pipeline:

```
S3 raw (多品牌異質 CSV)
    ↓
讀 + 解析 + 套 brand mapper
    ↓
canonical schema 驗證
    ↓
合併 customer + candidate products
    ↓
S3 cleaned (LLM 可直接用的 dataset CSV)
```

產出 `CleaningReport`(rows_in / rows_out / errors / mapping_used)寫進 `PipelineJob` 對應欄位。

## 2. 進入此 phase 前的前置條件

✅ Phase 0 完成:三層架構 + 4 services + 4 tables + 端到端 mock pipeline 跑通
✅ Phase 1 完成:Bedrock Sonnet 4.5 真 LLM 整合,`ANALYZER_MOCK_MODE=false` 可跑
⏸ **本 phase 啟動前必須收到的東西**:
   - 真實業務資料樣本(各品牌 raw CSV 至少各 1 份)
   - 客戶資料樣本(customers/customers.csv)
   - 業務確認 canonical schema 該包含哪些欄位

## 3. 必須先跟業務 / PO 釐清的問題

### Q1:Raw CSV 各品牌格式差異到什麼程度?

```
情境 A: 全部品牌格式 100% 一致
   → 一個 default mapper 就夠

情境 B: 不同品牌欄位名 / 型別 / 編碼不同
   → 每品牌一個 mapper class,brand → mapper 註冊表

情境 C: schema 會持續演化(新增欄位 / 改名)
   → 加 schema version 偵測 + 變更告警
```

### Q2:Canonical schema 該包含哪些欄位?

目前 `schemas/canonical.py` 已有 `CanonicalProduct` 雛形,**待業務確認以下欄位是否符合需求**:

```python
class CanonicalProduct:
    sku: str
    product_name: str
    price: float            # 是否需要 currency?
    stock: int              # 是否需要 region 維度?
    category: str           # enum?自由文字?
    brand: str
    extra: dict             # 各品牌獨有欄位
```

可能需要加的欄位(待確認):

- `currency`(多幣別?)
- `cost`(成本價,做毛利分析?)
- `tags`(分類標籤?)
- `available_regions`(地區別?)
- `release_date`(新品 / 舊款?)
- `image_url` / `description`(LLM 也許用得到?)

### Q3:Merged dataset 結構(給 LLM 看的版本)

`schemas/canonical.py` 的 `MergedDataset` 假設「per-customer 一份」,內容為:

```python
{
  "customer": CanonicalCustomer (1 個),
  "candidate_products": [CanonicalProduct]  (N 個)
}
```

**待確認**:
- 候選商品池怎麼決定?全部 / 該客戶 segment 適合的 / 該月份新上架的?
- 是要 N=10? 50? 100?(影響 prompt token 數)
- 客戶側要不要附歷史購買紀錄?

### Q4:資料品質檢查層級

```
L1: Pydantic schema 驗證(必填 / 型別)         ← POC 必做
L2: 業務規則(price > 0, stock >= 0, sku 格式)  ← POC 推薦做
L3: 跨筆檢查(sku 重複 / 外鍵 / 上下游一致)    ← Phase 2 再做
L4: 統計監測(本月新增量異常)                   ← Production 做
```

POC 預設 L2,確認業務需求後決定。

### Q5:錯誤處理策略

某筆 row 驗證失敗時:

```
A. 整個 ETL 中止 (strict)
B. 跳過該筆,記在 CleaningReport.errors,繼續
C. 部分修正後繼續(用預設值補)
```

POC 預設 B(部分容錯),production 視情境決定。

## 4. 工作項目(順序執行)

### 4.1 收集資料樣本(此 phase 阻塞點)

- [ ] 業務或工程 owner 提供至少 1-2 個品牌的真實 raw CSV(至少 5-10 筆 sample)
- [ ] 真實 customers.csv sample(至少 5 筆,涵蓋不同 segment)
- [ ] 放進 `products/{category}/{brand}/{year}/{month}/products.csv` 對應位置
- [ ] 跑 `scripts/localstack/init-buckets.sh` 重新 sync 進 LocalStack

### 4.2 設計 canonical schema(跟業務 review 後修)

- [ ] 在 `schemas/canonical.py` 確認 / 擴充 `CanonicalProduct` 欄位
- [ ] 加 `validators` 做 L2 業務規則(field_validator + model_validator)
- [ ] 跟業務 walk through 一次確認

### 4.3 實作 brand mapper 抽象

- [ ] 新增 `services/cleaner/mappers/` 目錄(或 `services/dataset/mappers/`)
- [ ] `base.py`:`BrandMapper` Protocol(定義 `transform(raw_row) -> CanonicalProduct`)
- [ ] `default_mapper.py`:identity mapping(假設欄位對得上)
- [ ] `registry.py`:`brand → mapper` 對照表
- [ ] (有需要)`brand_a_mapper.py`、`brand_b_mapper.py` 等實作

### 4.4 實作 `DatasetService.prepare()` 真邏輯

- [ ] 用 `S3Service.get_text()` 讀 raw CSV
- [ ] `pandas.read_csv(StringIO(csv_text))` parse
- [ ] 用對應 brand mapper transform 每一 row
- [ ] Pydantic 逐筆驗證,errors 收集到 `CleaningReport`
- [ ] 處理 customer 資料(讀 customers.csv,找對應 customer_id 的 row)
- [ ] 篩選 candidate products(規則待 Q3 確認)
- [ ] 組裝 `MergedDataset` Pydantic 物件
- [ ] `MergedDataset.model_dump_json()` 或 `to_csv()` 寫進 S3 cleaned bucket
- [ ] 回傳 `(cleaned_key, CleaningReport)`

### 4.5 把 `CleaningReport` 寫進 `PipelineJob`

- [ ] 修 `PipelineService.run()` 把 `CleaningReport` 寫進 `pipeline_job.cleaning_report`(JSONB)
- [ ] 同時 update `rows_input` / `rows_output` / `rows_failed` hot column
- [ ] 加 API endpoint `GET /pipelines/{id}/cleaning-report` 回完整報告

### 4.6 修改 `AgentService.analyze` 餵真實 dataset

- [ ] `_real_analyze()` 從 S3 cleaned 讀 dataset
- [ ] 把 dataset 內容塞進 prompt(注意 token 量)
- [ ] 等 Phase 2 PromptVariant 接好後再做正式 prompt

### 4.7 整合測試

- [ ] 跑 `POST /pipelines/run` 真實資料 → 看 cleaning report 內容合理嗎
- [ ] 跑同個 customer 多次 → recommendation 品質穩定嗎
- [ ] 跑壞資料(故意刪欄位 / 改型別)→ ETL 容錯機制正常嗎

## 5. 不在此 phase 做的事(留給未來)

- ❌ Schema discovery(用 LLM 自動產 mapping)— 等多品牌 + 格式變動頻繁才考慮
- ❌ Parquet 輸出 — POC 階段 CSV 夠用,> 100K rows 才考慮
- ❌ DuckDB SQL transform — 同上
- ❌ Schema 變更自動偵測 / migration — Phase 5 production hardening
- ❌ Cross-batch dedup / 跨檔一致性 — L3 級別,production 才做
- ❌ ETL 視覺化 dashboard — 業務真有需求才做

## 6. 啟動下個 session 的指令

```bash
# 1. 確保 docker infra 還活著
docker compose -f docker-compose.dev.yml ps

# 2. 確認 lab credentials 沒過期(過期就 refresh)
./scripts/refresh-lab-creds.sh

# 3. 啟動 FastAPI
set -a && source .env.local && set +a && unset AWS_PROFILE
uv run uvicorn recommender.main:app --reload

# 4. 確認 health 通
curl http://localhost:8000/health/ready

# 5. 開始填 src/recommender/services/dataset_service.py 的 prepare() method
```

## 7. 關鍵 references

| 資源 | 用途 |
|------|------|
| [architecture.md](../architecture.md) | 整體架構與設計原則 |
| `src/recommender/services/dataset_service.py` | 此 phase 主要要修改的檔案 |
| `src/recommender/schemas/canonical.py` | 要擴充欄位的 schema |
| `src/recommender/schemas/cleaning.py` | 已存在的 CleaningReport schema |
| `src/recommender/models/job.py` | PipelineJob 已有 cleaning 統計欄位 |
| Pandas docs | https://pandas.pydata.org/docs/ |
| LangChain CSV loader | 之後 prompt 要餵 CSV 可參考 |

## 8. 下個 session 第一個動作建議

打開 `dataset_service.py` 看現有 stub:

```python
# src/recommender/services/dataset_service.py
async def prepare(self, customer_id, brand, month) -> tuple[str, CleaningReport]:
    """
    TODO Phase 1.5: 實作 ETL
    1. 用 self.s3.list_objects() 找 raw CSV
    2. pandas.read_csv 讀進來
    3. 套 brand mapper 轉 canonical
    4. JOIN customer
    5. 篩選 candidate products
    6. 寫 cleaned CSV
    7. 回傳 cleaned key + CleaningReport
    """
    ...
```

接著把 Q1-Q5 5 個問題拿去問業務 / PO,等到答案再開始實作。

---

## 9. 實際產出 (Outcome)

> 紀錄日期:2026-05-06
> 狀態:✅ 完成,但實作 scope 與本文件原計畫不同

### 9.1 為什麼 scope 變了

**原計畫**:把 `DatasetService.prepare()` 從 stub 變成真 ETL,給「per-customer LLM 推薦」(`POST /pipelines/run`)用。輸出格式設計成 `MergedDataset`(`{ customer, candidate_products[] }`)。

**實際發現**(看到 sales 給的 4 月 xlsx 後):
1. **客戶 = 經銷商**(B2B 批發),不是消費者 — 這 reframe 改變整個 schema 解讀
2. **資料節奏 = 每月一份**(績效追蹤 + 月銷售)— 沒有「日級交易明細」可言
3. **拿到的是 dashboard pivot 表**(而非 transaction-level extract)— 已被聚合過,無法反推訂單級
4. **業務 PO 提的 prompt** 大多是「跨所有經銷商的月度市場分析」,而非「給某客戶推薦某商品」

→ 結論:Per-customer recommendation 這條線在 POC 階段沒有對應的 raw data,**不應該硬走原計畫**。改建月度跨經銷商分析 module。

### 9.2 實際建出來的東西

`SalesAnalysisService` (`src/recommender/services/sales_analysis_service.py`)+ `/analyses/sales/*` REST API,完整端到端跑通(raw S3 → 3 ETL → Bedrock Sonnet 4.5 → cleaned S3 markdown brief)。詳見 [architecture.md §5.5](../architecture/architecture.md) + [§7.2](../architecture/architecture.md)。

### 9.3 原計畫 Q1-Q5 對應到實際決策

| 原問題 | 原計畫的問法 | 實際決策 |
|-------|-----------|--------|
| **Q1** Raw CSV 各品牌格式差異 | 想做 brand mapper 機制 | N/A — sales xlsx 已是聚合表,無 brand-specific raw |
| **Q2** Canonical schema 該包含哪些欄位 | 給 `CanonicalProduct` 加欄位 | 走「**演算法寫死 col index**」,不抽 canonical schema(POC 階段穩定就行) |
| **Q3** Merged dataset 結構 | per-customer 一份 | 改成「**3 份 cross-dealer aggregated table**」(region×category / dealer-tier / cross-sell-gaps) |
| **Q4** 資料品質檢查層級 | L1-L4 | L1 + L2 用「pivot 表 col index 寫死」+「`pd.isna()` 容錯」帶過,L3-L4 不做 |
| **Q5** 錯誤處理策略 | strict / skip / fix | **跳過容錯**(NaN row、未認識課別、未認識 dealer 都 silent skip),POC 不阻塞 |

### 9.4 業務脈絡關鍵點(後續 session 一定要知道)

這些 fact 已存進 memory(`~/.claude/projects/.../memory/`):

| Memory | 重點 |
|--------|------|
| `project_本公司_customer_means_dealer.md` | 本公司「客戶」= 經銷商,所有 schema 中 customer_id 都指經銷商 ID |
| `project_data_cadence_monthly.md` | 資料每月一份,prompt 寫「今日」其實 = 「本月」 |
| `feedback_etl_first_llm_last.md` | 演算法先聚合,LLM 只做 narrative,不讓 LLM 算數 |
| `feedback_two_tier_parsing.md` | 預設用演算法,格式漂移才 fallback LLM(POC 不實作 fallback) |

### 9.5 PO 拍板的業務規則(寫死在 sales_analysis_service.py 頂部 const)

| 規則 | 值 |
|------|---|
| **層級門檻** | S≥50萬 / A=10-50萬 / B=3-10萬 / C<3萬 |
| **對應動作** | S=業務電話+客製EDM / A=標準EDM+LINE / B=群發EDM / C=季度喚醒 |
| **品類 mapping**(7→6) | 平板→通訊、應用週邊→配件、其他直接對應 |
| **區域 mapping**(5→4) | 企業客戶業務處→專戶,其他直接對應 |

### 9.6 已知技術債(留給未來)

| 項目 | 描述 | 嚴重度 |
|------|------|------|
| ETL 寫死月份路徑 | `xlsx = Path("aws-s3/sales/2026/04/績效追蹤4月.xlsx")` 5 月會炸 | 🟡 5月來時要改 |
| CSV 沒寫 BOM | Excel 開中文 CSV 會亂碼(`encoding="utf-8"` 沒 sig) | 🟢 1 行 fix |
| 統編欄空著 | 待 IT 提供客戶 master | 🟢 PO ack 過 |
| LLM hallucinate 衍生指標 | LLM 自算客單價、編造「2 週見效」等時程 | 🟡 production 前要加 negative constraint |
| 沒 `analyses` table | S3 = state,無法做 audit log / version diff | 🟡 production 要補 |
| 沒 SharePoint sync 腳本 | 月度資料目前手動 mv 進 aws-s3/ | 🟡 5 月來時要寫 |

### 9.7 5 月資料來時要做什麼

```
1. 把新檔放進 aws-s3/sales/2026/05/(SharePoint sync 腳本待實作,暫時手動 mv)
2. 寫 _manifest.json(新月份必填,logical key 不變)
3. 跑 init-buckets.sh 同步 S3
   docker exec marketing-poc-localstack /etc/localstack/init/ready.d/init-buckets.sh
4. POST /analyses/sales body { month: "2026-05" }
   (service 已接受 month 參數,不需要改任何 const)
5. 等 ~50s 後 GET /analyses/sales/2026-05/artifacts/market-narrative
```

**注意**:`scripts/etl/*.py` 那 3 個 standalone CLI script 寫死了 4 月路徑,**API 不依賴它們**。它們純粹是 dev iteration 用的快速腳本,可以選擇刪除或讓它們也讀 month 參數。
