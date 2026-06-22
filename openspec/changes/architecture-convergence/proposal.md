# architecture-convergence

## Why

專案在 cleaner→recommender 重構（commit 8fc336c）後，FastAPI + LangChain 主鏈已經跑通，但留下四類「架構收斂債」。不處理的話，每加一個新模組就會複製一次錯誤模式，維護成本隨檔案數線性放大：

1. **分層邊界滲漏** — repository 讀全域 settings、API 直接打 repository、service 回傳 ORM 物件，三層架構（`api/` → `services/` → `repositories/`）的契約名存實亡：
   - `repositories/recommendation_repo.py:36` 的 `create_from_agent_output` 直接讀 `settings.bedrock_model_id`，違反 coding-rules.md「Repository 純 CRUD」原則 —— repository 不該知道「用哪個 model」這種業務決策。
   - `api/recommendations.py:11`（`get_recommendation`）、`api/recommendations.py:18`（`list_by_customer`）、`api/evaluations.py:22`（`get_evaluation`）、`api/evaluations.py:30`（`list_by_recommendation`）四個 read 端點直接注入 repository、自行拋 `HTTPException`，繞過 service 層。
   - `services/pipeline_service.py:37-45` 回傳 `PipelineJob` ORM，由 `api/pipelines.py:32` 的 `_to_response` 在 API 層自己轉；`services/evaluation_service.py:35` 回傳 `Evaluation` ORM，靠 `response_model` 隱式轉 —— 違反 coding-rules.md「Service 不回傳 SQLModel ORM 物件給 API 層」。

2. **一致性債** — 同一種「查無資源」錯誤有三套寫法並存（`repositories/job_repo.py:42` 拋 `ValueError`、service 層拋 `errors.py` 的 `NotFoundError`、API 層直接拋 `HTTPException`），而 `main.py:65` 早已註冊好 `NotFoundError` 全域 handler 卻只有部分路徑走它。另外 `services/agent_service.py:85` 用 `getattr(settings, "bedrock_guardrail_id", None)` 讀一個 `config.py` Settings 裡**根本沒宣告**的欄位 —— 因為 `config.py:12` 設了 `extra="ignore"`，就算 env 設了 `BEDROCK_GUARDRAIL_ID` 也會被靜默吞掉，guardrail 永遠不生效且無任何 warning。

3. **死碼與孤兒** — `schemas/canonical.py`（`CanonicalProduct` / `CanonicalCustomer` / `MergedDataset`）無任何 service import，純死碼；prompt 管理雙軌並存：runtime 實際走 `.md` 檔（`prompts.py` + `chains/` 的 `*_PROMPT_VERSION` 常數），但 `services/agent_service.py:70` 的 `_select_prompt_variant` 是 `raise NotImplementedError` 死路、`variant_id` 永遠是 `None`，留著只會誤導後人以為 DB prompt 路徑可用。

4. **文件與測試真空** — `docs/architecture/architecture.md` 仍描述已不存在的 `SalesAnalysisService` / `api/analyses.py` / `/analyses/sales/*` 端點（檔案已刪除，只剩 pycache），而實際存在的 `chains/`、`llm.py`、`prompts.py`、`errors.py`、`deps.py`、`PromoForecastService`（451 行孤兒服務）完全未記載 —— 文件與現況的偏差會讓任何新 session 的架構理解直接錯誤。`tests/` 目錄完全不存在，但 `pyproject.toml:67-69` 已設好 `testpaths=["tests"]`、`asyncio_mode="auto"` 並裝了 pytest / pytest-asyncio —— 測試基建備好了卻零測試，任何 refactor（包括本次）都無迴歸保護網。

本次變更鎖定「標準」範圍：**只收斂、不加 feature**，每項變更最小、可獨立驗證，對齊 CLAUDE.md Simplicity First 與 forward-only migration 原則。

## What Changes

### A. 修補分層邊界滲漏

- **A1** — `recommendation_repo.create_from_agent_output` 不再讀 `settings.bedrock_model_id`，改為新增必填參數 `model_id: str` 由 caller 傳入；上游呼叫點 `services/pipeline_service.py:84` 改為傳入 `settings.bedrock_model_id`（service 層讀 config 是合法的）。
- **A2** — 補一層薄 read service：新增 `RecommendationService`（`get` / `list_by_customer` 兩個 read 方法），`EvaluationService` 補 `get` / `list_by_recommendation` 兩個 read 方法。API 改注入 service；查無資源時 service 拋 domain `NotFoundError`，不在 API 層拋 `HTTPException`。**不做 interface / 抽象基類**，就是普通 service class。
- **A3** — service 一律回傳 Pydantic DTO，不回傳 ORM：`PipelineService.create_job` / `get_job` 改回 `schemas/pipeline.py` 的 `JobResponse`（轉換邏輯從 `api/pipelines.py:32` 的 `_to_response` 移進 service）；`EvaluationService.evaluate` 與新增的 read 方法回 `schemas/public.py` 的 `EvaluationPublic`；`RecommendationService` 回 `RecommendationPublic`。

### B. 一致性債

- **B1** — 錯誤型別統一：repository / service 一律拋 `errors.py` 的 `NotFoundError`，API 層不再自己拋 `HTTPException`，全部交給 `main.py:65` 已註冊的全域 exception handler 轉 404。`repositories/job_repo.py:42` 的 `ValueError` 改 `NotFoundError`。
- **B2** — guardrail 設定不再靜默失效：在 `config.py` 的 `Settings` 正式宣告 `bedrock_guardrail_id: str | None = None` 與 `bedrock_guardrail_version: str | None = None`；`services/agent_service.py:85,89` 的兩處 `getattr(settings, ...)` 改為直接讀 `settings.bedrock_guardrail_id` / `settings.bedrock_guardrail_version`。

### C. 清死碼與孤兒

- **C1** — 刪除 `src/recommender/schemas/canonical.py`，並同步移除 `schemas/__init__.py:2-6` 的 import 與 `__all__` 內的 `CanonicalProduct` / `CanonicalCustomer` / `MergedDataset`（`schemas/__init__.py:22-24`）。
- **C2** — 收斂 prompt 雙軌：確立 **`.md` 檔為唯一 runtime prompt 來源**（`prompts.py` 載入 + `chains/` 的 `*_PROMPT_VERSION` 常數）。移除 `agent_service.py:62-70` 的 `_select_prompt_variant`（`NotImplementedError` 死路）與 `analyze()` 內相關 dead branch；`analyze()` 簡化為回傳 `RecommendationOutput`（不再回 `(output, variant_id)` tuple），`pipeline_service.py:77` 同步調整。`PromptVariant` 表**保留不 drop**（forward-only，留作未來 A/B 基建），在架構文件標註其「目前未連通」狀態。
- **C3** — HubSpot 6 欄（`models/recommendation.py:51-58` 的 `hubspot_sync_status` / `hubspot_contact_id` / `hubspot_note_id` / `hubspot_synced_at` / `hubspot_sync_error` / `hubspot_sync_retries`）**保留不動**，僅在架構文件的資料模型段標註為 Phase 4 reserved。詳見 Out of Scope。

### D. 文件與測試真空

- **D1** — 同步 `docs/architecture/architecture.md` 對齊現況：移除已不存在的 `SalesAnalysisService` / `api/analyses.py` / `/analyses/sales/*` 描述（或標註為已下線、實作見 git history）；補記實際存在的 `chains/`（LCEL chain factory）、`llm.py`（`lru_cache` Bedrock builder）、`prompts.py`（`.md` 載入）、`errors.py`（domain 例外）、`deps.py`（DI 集中）、`PromoForecastService`（451 行、尚未接 API 的孤兒服務）。
- **D2** — 建立 `tests/` 並補兩類測試：
  - **mock_mode e2e**：`ANALYZER_MOCK_MODE=true` 走完整 pipeline（POST `/pipelines/run` → 查 job → 查 recommendation → POST `/evaluations/{id}` → 查 evaluation），全程不打 Bedrock。
  - **ETL 純函式單元測試**：`services/promo_forecast_service.py` 的 deterministic 純函式（`_filter_zhuanhu_dealers` / `_normalize_dealer_id` / `_classify_legal_categories` / `_build_reasoning` / `_rank_opportunities` 等）、`evaluation_service._build_inputs`（:91）的聚合邏輯。chain 層測試用 LangChain fake chat model 注入（`chains/` 已設計成 `build_xxx_chain(llm)` 接受注入）。

## Out of Scope（本次明確不做）

| 項目 | 為什麼不做 |
|------|-----------|
| `PromoForecastService` 接 API router / DI、33 家統編 hardcode（`promo_forecast_service.py:85`）外移 | 屬於新功能接線，不是架構收斂；接 API 涉及新 endpoint 設計，應另開 change。本次只在 D1 文件記載其存在與孤兒狀態，並在 D2 為其純函式補測試（測試不需要接 API） |
| drop `PromptVariant` 表 | forward-only migration 原則（safety.md：downgrade / drop 會掉歷史資料且不可重建）。表留作未來 prompt A/B 基建，成本只是一張閒置表 |
| drop HubSpot 6 欄位 | 同上 forward-only 保守原則。drop 欄位需要 migration、風險高、且 Phase 4 HubSpot 同步確定會用到（`hubspot_sync_status=pending` 已在寫入路徑使用，`list_pending_hubspot_sync` 已存在）。收益（省 5 個閒置欄位）遠小於風險 |
| 新增任何 feature、prompt A/B 實際選擇邏輯 | 本次是收斂不是擴充。A/B hash-select 等 PromptVariant 表有資料、有真實需求再實作 |
| 新增 DB migration | 本次所有變更都不碰 schema。`alembic current` 前後必須一致（驗證項之一） |
