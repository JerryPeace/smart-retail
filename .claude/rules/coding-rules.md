---
alwaysApply: true
description: "FastAPI + SQLModel + LangChain 三層架構的 coding 規範"
---

# Coding Rules

## Before You Code

- 永遠先讀懂相關檔案再提改動，不要對沒打開的程式碼瞎猜
- 實作新模組前先讀 `docs/architecture/architecture.md`
- 充分檢視既有風格、慣例、抽象，再寫新 code

## Do

- 直接實作改動 — 不要只「建議」
- 重用既有抽象。遵守 DRY
- 簡單為先。正確的複雜度 = 完成當前任務需要的最少
- 一次性腳本 / 測試檔，任務完成後清理掉
- 動作獨立時用 parallel tool calls

## Do NOT

- 加沒被要求的 feature、refactor、「改進」
- 對沒動的 code 補 docstring、註解、type annotation
- 為不可能發生的情境加 error handling
- 為一次性操作建 helper / utility / 抽象
- 用 placeholder 或猜參數值 — 不知道就調查
- 因為 token 預算提早收工。要堅持

---

# 架構：API → Service → Repository → SQLModel

照 `docs/architecture/architecture.md` 的三層架構：

```
src/recommender/
├── api/             # Layer 1 — HTTP 邊界(FastAPI router)
├── services/        # Layer 2 — 業務邏輯(編排、ETL、LLM 呼叫)
├── repositories/    # Layer 3 — DB CRUD(SQLModel)
└── models/          # SQLModel 資料模型
```

### 每層的職責

- **API (`api/*.py`)** — 接 HTTP、做 schema 驗證、呼叫 Service、回 response。**不要直接碰 DB**
- **Service (`services/*.py`)** — 業務邏輯、跨 repository 編排、外部服務(S3 / Bedrock)整合
- **Repository (`repositories/*.py`)** — 純 CRUD。一個 model 一個 repo
- **Model (`models/*.py`)** — SQLModel table，搭配 Alembic migration

### 依賴注入

用 FastAPI 的 `Depends()`，不要用全域 singleton：

```python
# OK
def list_recommendations(
    repo: RecommendationRepository = Depends(get_recommendation_repo),
):
    return repo.list_all()

# NOT OK
recommendation_repo = RecommendationRepository()  # 模組層級全域物件
```

## ETL 與 LLM 的分工(專案核心原則)

> **演算法處理資料、LLM 處理敘事。** 這是本專案的核心信條。

| 任務 | 用什麼 | 原因 |
|------|--------|------|
| 算總和 / 平均 / 排名 | **Python / SQL** | LLM 算數會錯，且每次成本不可控 |
| 月銷售聚合、跨經銷商比較 | **Python / SQL**(在 service 層做) | 結構化資料不該送進 LLM |
| 解析格式穩定的 xlsx | **演算法 first**(openpyxl / pandas) | LLM 解析貴又不穩 |
| 解析格式漂移的 xlsx | LLM **fallback only** | 演算法失敗才 fallback,不要預設用 LLM |
| 寫推薦理由、信心度敘事 | **LLM**(LangChain → Bedrock) | 這才是 LLM 的長處 |

具體規則：
- **不要把原始 raw data 餵給 LLM 要它算總和。** 先在 Service / Repository 把資料聚合成「已算好的表」，再把表丟給 LLM 寫 narrative
- **每個 LLM 呼叫都要有 mock 路徑**(走 `ANALYZER_MOCK_MODE` env var)，否則本機開發要燒錢
- **prompt 走 versioning**(看 `prompt_variants` 表)，不要把 prompt 寫死在程式碼裡

## Naming

| 類型 | 格式 | 範例 |
|------|------|------|
| Repository | `{Module}Repository` | `RecommendationRepository` |
| Service | `{Module}Service` | `PipelineService` |
| Pydantic Request | `{Action}{Module}Request` | `CreateRecommendationRequest` |
| Pydantic Response | `{Module}Response` | `RecommendationResponse` |
| SQLModel Table | 名詞 | `Recommendation`, `PromptVariant` |

## 常見錯誤(別犯)

- 在 API 層直接呼叫 SQLModel session 跑 query — 一律走 repository
- Service 回傳 SQLModel ORM 物件給 API 層 — 應該轉 Pydantic response model
- Repository 包含業務邏輯(if 判斷、外部呼叫) — 純 CRUD 才對
- 把 raw xlsx 完整送進 LLM 要它算 — 先聚合再送
- Hardcode prompt 內容 — 走 `prompt_variants` 表 + version
- 跳過 alembic 直接改 schema — 一律走 migration
