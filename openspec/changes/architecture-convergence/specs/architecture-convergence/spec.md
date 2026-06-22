# Spec: architecture-convergence

本規格定義收斂後的四份「契約」：分層契約、錯誤處理契約、settings 契約、測試契約。實作完成後，任何違反下列 Requirement 的 code 視為未通過驗收。

## ADDED Requirements

### Requirement: 分層契約 — API 層只依賴 service

API router（`src/recommender/api/*.py`）SHALL 只注入 service（經 `deps.py` 的 `*ServiceDep`），SHALL NOT 注入 repository、SHALL NOT 執行 ORM→DTO 轉換、SHALL NOT 拋 `HTTPException`。

#### Scenario: read 端點走 service
- **WHEN** client 呼叫 `GET /recommendations/{id}`、`GET /recommendations/by-customer/{id}`、`GET /evaluations/{id}`、`GET /evaluations/by-recommendation/{id}`、`GET /pipelines/{id}`
- **THEN** router 僅呼叫對應 service 方法並回傳其結果，函式體內無 `if x is None: raise` 分支

#### Scenario: API 層靜態檢查
- **WHEN** 執行 `grep -rn "HTTPException\|RepoDep" src/recommender/api/`
- **THEN** `HTTPException` 為 0 筆；`*RepoDep` 注入為 0 筆

### Requirement: 分層契約 — Service 層回傳 Pydantic DTO

Service 對 API 層暴露的公開方法 SHALL 回傳 Pydantic DTO（`RecommendationPublic` / `EvaluationPublic` / `JobResponse`），SHALL NOT 回傳 SQLModel ORM 物件。Service 是唯一允許讀取 `settings`、執行跨 repository 編排、與做 ORM→DTO 轉換的層。

#### Scenario: 各 service 回傳型別
- **WHEN** 檢視 `RecommendationService.get/list_by_customer`、`EvaluationService.evaluate/get/list_by_recommendation`、`PipelineService.create_job/get_job` 的回傳型別註記
- **THEN** 分別為 `RecommendationPublic`（含 list）、`EvaluationPublic`（含 list）、`JobResponse`，無任何 `-> Recommendation` / `-> Evaluation` / `-> PipelineJob`

#### Scenario: 薄 read service 不過度設計
- **WHEN** 檢視 `RecommendationService` 與 `EvaluationService` 的 read 方法
- **THEN** 為普通 class 的普通 async 方法（建構子注入 repo），不存在 interface、抽象基類、generic base 等新抽象

### Requirement: 分層契約 — Repository 層純 CRUD

Repository（`src/recommender/repositories/*.py`）SHALL 只做 CRUD：不 import `recommender.config`、不含業務判斷、不呼叫外部服務。業務參數（如 `model_id`）SHALL 由 caller 以參數傳入。

#### Scenario: create_from_agent_output 不讀 settings
- **WHEN** `PipelineService.run()` 寫入 recommendation
- **THEN** `create_from_agent_output(..., model_id=...)` 的 `model_id` 由 service 傳入（值為 `settings.bedrock_model_id`，與收斂前寫入內容一致）
- **AND** `grep -rn "from recommender.config import settings" src/recommender/repositories/` 為 0 筆

### Requirement: 錯誤處理契約 — 單一 NotFoundError 流

「查無資源」SHALL 只有一種表達：`recommender.errors.NotFoundError`。repository 的 read 方法（`get` 系列）回 `None`，由 service 判斷後拋 `NotFoundError`；repository 寫入路徑的前置查找失敗（如 `job_repo.update_status`）直接拋 `NotFoundError`。API 層不拋、不接 —— 例外由 `main.py:65` 的全域 handler 轉 HTTP 404（`{"detail": str(exc)}`），未預期例外由 `main.py:71` 的 handler 轉 500。

#### Scenario: 查無資源回 404
- **WHEN** client 以不存在的 id 呼叫 `GET /pipelines/{id}`、`GET /recommendations/{id}`、`GET /evaluations/{id}` 或 `POST /evaluations/{id}`
- **THEN** 回 `404` + JSON body `{"detail": "... not found"}`

#### Scenario: repository 不再拋 ValueError 表達查無
- **WHEN** `JobRepository.update_status` 收到不存在的 `job_id`
- **THEN** 拋 `NotFoundError`，且 `grep -rn "raise ValueError" src/recommender/repositories/` 為 0 筆

#### Scenario: BackgroundTask 例外不經 HTTP handler
- **WHEN** `PipelineService.run()`（BackgroundTask 內）遭遇例外
- **THEN** 維持既有行為：`logger.exception` + job 標記 `failed` + re-raise，不期待 HTTP handler 介入

### Requirement: Settings 契約 — guardrail 欄位正式宣告

`config.py` 的 `Settings` SHALL 宣告 `bedrock_guardrail_id: str | None = None` 與 `bedrock_guardrail_version: str | None = None`。程式碼 SHALL NOT 以 `getattr(settings, ...)` 讀取任何 Settings 欄位（那是「欄位可能不存在」的掩護寫法，會讓設定靜默失效）。

#### Scenario: env var 正常綁定
- **WHEN** 環境設定 `BEDROCK_GUARDRAIL_ID=gr-xxx` 後啟動 app
- **THEN** `settings.bedrock_guardrail_id == "gr-xxx"`，且 `AgentService._guardrail_config()` 回含 `guardrailIdentifier` 的 dict（version 未設時 fallback `"DRAFT"`）

#### Scenario: 未設定時行為不變
- **WHEN** 未設 guardrail env var
- **THEN** `_guardrail_config()` 回 `None`，LLM 呼叫不帶 guardrailConfig（與收斂前一致）

#### Scenario: 全 codebase 無 getattr(settings
- **WHEN** 執行 `grep -rn "getattr(settings" src/recommender/`
- **THEN** 0 筆

### Requirement: Prompt 契約 — .md 檔為唯一 runtime 來源

Runtime prompt SHALL 只來自 `prompts/{module}/{version}.md`，由 `prompts.py:load_system_prompt` 載入、版本由 `chains/` 的 `*_PROMPT_VERSION` 常數指定。`PromptVariant` 表與 `prompt_variant_repo` SHALL 保留（dormant、forward-only），但 SHALL NOT 存在指向它的 runtime 死路（`NotImplementedError` stub、永遠為 `None` 的 variant 參數鏈）。

#### Scenario: 死路移除
- **WHEN** 執行 `grep -rn "NotImplementedError" src/recommender/services/` 與 `grep -rn "prompt_variant_id" src/recommender/services/`
- **THEN** 皆為 0 筆；`AgentService.analyze()` 回傳 `RecommendationOutput`（非 tuple）

#### Scenario: 表保留且文件標註
- **WHEN** 檢視 alembic 與架構文件
- **THEN** 無 drop `prompt_variant` 的 migration；`docs/architecture/architecture.md` 標註該表為 dormant（未連通、留作 A/B 基建）

### Requirement: 資料模型契約 — forward-only、零 migration

本變更 SHALL NOT 新增、修改、刪除任何 DB schema。HubSpot 6 欄（`models/recommendation.py:51-58`）SHALL 原封保留並在架構文件標註 Phase 4 reserved。

#### Scenario: alembic 狀態不變
- **WHEN** 比對實作前後的 `alembic current` 輸出與 `alembic/versions/` 目錄
- **THEN** revision 相同、無新檔案

### Requirement: 文件契約 — architecture.md 反映真實結構

`docs/architecture/architecture.md` SHALL 與 `src/recommender/` 實際檔案結構一致：不描述已不存在的模組（`SalesAnalysisService` / `api/analyses.py` / `/analyses/sales/*`），且記載所有實際存在的頂層模組（`chains/`、`llm.py`、`prompts.py`、`errors.py`、`deps.py`、`PromoForecastService` 含其「未接 API 的孤兒」狀態）。

#### Scenario: 文件與檔案系統對齊
- **WHEN** 比對文件目錄樹與 `find src/recommender -name "*.py"` 輸出
- **THEN** 文件不含已刪除檔案；上述六個現存模組皆有對應段落或目錄樹條目

### Requirement: 測試契約 — 覆蓋層級與 mock 策略

`tests/` SHALL 存在且 `pytest` 全綠，覆蓋三個層級，全程零 Bedrock 呼叫（零花費）：

1. **e2e（HTTP→service→repo→DB）**：mock mode（`ANALYZER_MOCK_MODE=true`）下走完整 pipeline 與 evaluation 流程 + 404 負路徑。DB 用 docker-compose dev Postgres。
2. **ETL 單元（純函式）**：`promo_forecast_service` deterministic 函式與 `evaluation_service._build_inputs`，無 DB、無網路 —— 守住「ETL First, LLM Last」的演算法聚合層。
3. **chain 組裝（fake LLM 注入）**：`build_recommendation_chain` / `build_judge_chain` 以 fake chat model 注入，驗 prompt 變數與輸出型別 contract。

#### Scenario: mock e2e 不打 Bedrock
- **WHEN** `ANALYZER_MOCK_MODE=true` 下跑 e2e 測試
- **THEN** pipeline 走到 `done`、evaluation 的 `judge_model_id == "mock"`，無任何 AWS Bedrock 網路呼叫

#### Scenario: 單元測試獨立於基礎設施
- **WHEN** 在無 docker、無 AWS 憑證的環境單獨跑 `pytest tests/test_etl_units.py tests/test_chains.py`
- **THEN** 全部通過

#### Scenario: 測試守住錯誤處理契約
- **WHEN** e2e 負路徑測試以不存在的 id 打 read 端點
- **THEN** 收到 404 + `{"detail": ...}`，證明 NotFoundError → 全域 handler 鏈路有效
