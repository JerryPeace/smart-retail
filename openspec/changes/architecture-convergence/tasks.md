# architecture-convergence — Tasks

> 排序原則：Python 三層由下而上（config/models/schemas → repositories → services → api → docs → tests → verification），每個 phase 完成後 app 都應可啟動（`uvicorn recommender.main:app`），確保變更可獨立驗證。
>
> ⚠️ 本次**不應產生任何新 migration**。任何 task 做到一半發現「好像需要 migration」，停下來和使用者確認 —— 那代表偏離了計劃範圍。

## Phase 1 — config / schemas（B2, C1）

- [x] **1.1 (B2)** `src/recommender/config.py`：`Settings` Bedrock 段新增 `bedrock_guardrail_id: str | None = None`、`bedrock_guardrail_version: str | None = None`。
      ✅ 判準：`python -c "from recommender.config import settings; print(settings.bedrock_guardrail_id)"` 輸出 `None`；設 `BEDROCK_GUARDRAIL_ID=test` 後再跑輸出 `test`（證明 env 綁定生效、不再被 `extra="ignore"` 吞掉）。
- [x] **1.2 (C1)** 刪除 `src/recommender/schemas/canonical.py`；`schemas/__init__.py` 移除 :2-6 的 canonical import 與 `__all__` 的 `CanonicalProduct` / `CanonicalCustomer` / `MergedDataset`（:22-24）。
      ✅ 判準：`grep -rn "canonical\|CanonicalProduct\|MergedDataset" src/ --include="*.py"` 僅剩 `dataset_service.py:35` 的註解字樣（非 import）；app 可正常 import。

## Phase 2 — repositories（A1, B1）

- [x] **2.1 (A1)** `repositories/recommendation_repo.py`：`create_from_agent_output` 新增必填 keyword 參數 `model_id: str`，內部 `model_id=settings.bedrock_model_id`（:36）改用參數；刪除 `from recommender.config import settings`（:5）。
      ✅ 判準：`grep -n "settings" src/recommender/repositories/recommendation_repo.py` 為 0 筆。（此刻 `pipeline_service.py:84` 呼叫會缺參數 —— 在 task 3.3 補上，Phase 2 結束前先一併改 caller 不留紅燈：見 3.3。）
- [x] **2.2 (B1)** `repositories/job_repo.py:42`：`raise ValueError(...)` 改 `raise NotFoundError(...)`，補 `from recommender.errors import NotFoundError`。
      ✅ 判準：`grep -rn "raise ValueError" src/recommender/repositories/` 為 0 筆。

## Phase 3 — services（A1, A2, A3, B1, B2, C2）

- [x] **3.1 (A2/A3)** 新增 `src/recommender/services/recommendation_service.py`：`RecommendationService(rec_repo)`，`get(rec_id) -> RecommendationPublic`（查無拋 `NotFoundError`）、`list_by_customer(customer_id, limit=20) -> list[RecommendationPublic]`。用 `RecommendationPublic.model_validate(orm)` 轉換。不做 interface / 基類。
      ✅ 判準：class 只有建構子 + 兩個 read 方法，無多餘抽象。
- [x] **3.2 (A2/A3)** `services/evaluation_service.py`：補 `get(eval_id) -> EvaluationPublic`、`list_by_recommendation(recommendation_id) -> list[EvaluationPublic]`；`evaluate()`（:35）改回 `EvaluationPublic`（`model_validate` 包住 repo 回傳的 ORM）。
      ✅ 判準：`evaluate` / `get` / `list_by_recommendation` 回傳型別註記皆為 Pydantic DTO，無 `-> Evaluation`。
- [x] **3.3 (A1/A3/B1)** `services/pipeline_service.py`：
      (a) `run()` 內 `create_from_agent_output(...)` 補傳 `model_id=settings.bedrock_model_id`（補 import settings）；
      (b) `_to_response` 邏輯自 `api/pipelines.py:32-43` 搬入為私有方法，`create_job` / `get_job` 改回 `JobResponse`；
      (c) `get_job` 查無時拋 `NotFoundError`（取代回 `None`）。
      ✅ 判準：`pipeline_service.py` 無任何方法回傳 `PipelineJob` 給 API 層使用（`run()` 內部操作 ORM 不算，它不回傳給 API）。
- [x] **3.4 (B2/C2)** `services/agent_service.py`：
      (a) `_guardrail_config()` 的兩處 `getattr(settings, ...)`（:85, :89）改直讀 `settings.bedrock_guardrail_id` / `settings.bedrock_guardrail_version or "DRAFT"`；
      (b) 刪除 `_select_prompt_variant`（:62-70）與 `analyze()` 內 variant dead branch（:46-48）；`analyze()` 回傳簡化為 `RecommendationOutput`（去 tuple）；
      (c) `__init__` 移除 `prompt_repo` 參數（C2 後零使用點）；docstring 同步修正（拿掉「從 DB PromptVariant 取 active prompt」的不實描述）。
      ✅ 判準：`grep -n "getattr(settings\|NotImplementedError\|prompt_repo" src/recommender/services/agent_service.py` 為 0 筆。
- [x] **3.5 (C2)** `services/pipeline_service.py:77`：配合 3.4(b) 調整 `analyze()` 解包（去 `prompt_variant_id`），刪除 :89-90 的 dead TODO。
      ✅ 判準：`grep -n "prompt_variant_id" src/recommender/services/pipeline_service.py` 為 0 筆。

## Phase 4 — API + DI（A2, A3, B1）

- [x] **4.1 (A2)** `deps.py`：新增 `get_recommendation_service(rec_repo) -> RecommendationService` provider 與 `RecommendationServiceDep`；`get_agent_service` 配合 3.4(c) 拿掉 `prompt_repo` 注入（`get_prompt_variant_repo` / `PromptVariantRepoDep` 保留 —— repo 本身不是死碼，是 dormant 基建）。
      ✅ 判準：app 啟動無 DI 錯誤；`deps.py` 仍是唯一 wiring 點。
- [x] **4.2 (A2/B1)** `api/recommendations.py`：兩個端點改注入 `RecommendationServiceDep`；刪除 `HTTPException` import 與 :13-14 的手拋 404。
      ✅ 判準：檔內無 `HTTPException`、無 repo 注入。
- [x] **4.3 (A2/B1)** `api/evaluations.py`：`get_evaluation`（:22）、`list_by_recommendation`（:30）改走 `EvaluationServiceDep`；刪除 `HTTPException` import 與 :25-26 手拋 404；刪除 `EvaluationRepoDep` 注入。
      ✅ 判準：檔內無 `HTTPException`、無 repo 注入。
- [x] **4.4 (A3/B1)** `api/pipelines.py`：刪 `_to_response`（:32-43）與 `HTTPException`（:2, :27-28）；`run_pipeline` 用 `job.job_id` 排 background task（service 已回 `JobResponse`）；`get_job` 直接 `return await service.get_job(job_id)`。
      ✅ 判準：`grep -rn "HTTPException" src/recommender/api/` 全 API 層為 0 筆。

## Phase 5 — docs（C2, C3, D1）

- [x] **5.1 (D1)** `docs/architecture/architecture.md` 結構同步：移除/標註已下線的 `SalesAnalysisService`、`api/analyses.py`、`/analyses/sales/*`（§2 目錄樹、§5.5、§7.x 資料流、§endpoint 清單、Phase 表 :610-637 等處）；補記 `chains/`（LCEL factory + `*_PROMPT_VERSION`）、`llm.py`（`lru_cache` Bedrock builder + lifespan 預熱）、`prompts.py`（`.md` 載入 + immutable version cache）、`errors.py`（domain 例外 → main.py handler）、`deps.py`（DI 集中）、`PromoForecastService`（451 行、尚未接 API、33 家統編 hardcode 的孤兒狀態）。
      ✅ 判準：`grep -n "SalesAnalysisService\|analyses.py" docs/architecture/architecture.md` 僅剩「已下線」的標註行；文件目錄樹與 `find src -name "*.py"` 實際輸出一致。
- [x] **5.2 (C2/C3)** 同文件資料模型段補兩個標註：(a) `prompt_variant` 表 dormant —— `.md` 檔為唯一 runtime prompt 來源，表保留作未來 A/B 基建；(b) `recommendation` 表 HubSpot 6 欄為 Phase 4 reserved，目前僅 `hubspot_sync_status=pending` 有寫入路徑。
      ✅ 判準：兩個標註可被 grep 到（`dormant`、`Phase 4 reserved`）。

## Phase 6 — tests（D2）

- [x] **6.1** 建 `tests/conftest.py`：在任何 `recommender` import 前設 `ANALYZER_MOCK_MODE=true` 環境變數；提供 `httpx.AsyncClient(transport=ASGITransport(app))` fixture。docstring 寫明前置條件：`docker compose up -d`（dev Postgres `localhost:5434`）。
      ✅ 判準：`pytest --collect-only` 成功收集。
- [x] **6.2** `tests/test_pipeline_e2e.py`（mock-mode、不打 Bedrock）：
      (a) 正路徑：`POST /pipelines/run` → 202 + job_id → 輪詢 `GET /pipelines/{id}` 至 `done` → `GET /recommendations/{rec_id}` 驗 mock fixture 欄位 → `POST /evaluations/{rec_id}` → 201 + `judge_model_id == "mock"` → `GET /evaluations/by-recommendation/{rec_id}` 含該筆；
      (b) 負路徑（驗 B1 鏈）：`GET /pipelines/999999`、`GET /recommendations/999999`、`GET /evaluations/999999` 皆 404 + `detail`。
      ✅ 判準：測試通過且測試期間 `ANALYZER_MOCK_MODE=true`（零 Bedrock 呼叫、零花費）。
- [x] **6.3** `tests/test_etl_units.py`：`promo_forecast_service` 純函式（`_filter_zhuanhu_dealers` / `_normalize_dealer_id` / `_classify_legal_categories` / `_build_reasoning` / `_rank_opportunities` / `opportunities_to_dataframe`）+ `evaluation_service._build_inputs` 聚合格式。全部無 DB / 無網路（⭐取捨 4：unit 不碰 DB）。
      ✅ 判準：單獨跑 `pytest tests/test_etl_units.py` 不需 docker 也通過。
- [x] **6.4** `tests/test_chains.py`：依 ⭐取捨 5（design.md §7.4 定稿）測 chain 組裝 contract —— 用自製 `FakeStructuredChatModel(GenericFakeChatModel)`（stub `bind_tools` 回 `self.bind(...)`）餵帶 `tool_calls` 的 `AIMessage` 注入 `build_recommendation_chain` / `build_judge_chain`，驗 prompt 變數齊備與輸出型別（recommendation→`RecommendationOutput`；judge→`{"parsed", "raw"}` dict）。**不可**直接用 `FakeListChatModel`（無 `bind_tools`、過不了 `with_structured_output`）。測 service 編排層另用 `RunnableLambda` 注入整條 chain（§7.4(a)）。
      ✅ 判準：測試通過、無 Bedrock 呼叫；不需 docker 單獨可跑。

## Phase 7 — Verification（全項完成後）

- [x] **7.1** `pytest` 全綠（前置：`docker compose up -d`）。
- [x] **7.2** **零 migration 驗證**：記下開工前 `alembic current` 輸出，完工後再跑一次，revision 必須相同；`git status alembic/versions/` 無新檔。若過程中任何人想加 migration → 停下與使用者確認。
- [x] **7.3** API 手動 smoke（mock mode，`ANALYZER_MOCK_MODE=true` 起 uvicorn）：
      - `POST /pipelines/run`（任意 customer_id/brand/month）→ 202
      - `GET /pipelines/{job_id}` → `done` 且 `recommendation_id` 非 null
      - `GET /recommendations/{rec_id}` → 200、`GET /recommendations/999999` → 404
      - `POST /evaluations/{rec_id}` → 201、`GET /evaluations/999999` → 404
      - response body 欄位與改動前一致（contract 不變）
- [x] **7.4** 邊界 grep 全套（design.md §3 驗收 grep）：
      - `grep -rn "HTTPException" src/recommender/api/` → 0 筆
      - `grep -rn "raise ValueError" src/recommender/repositories/` → 0 筆
      - `grep -rn "from recommender.config import settings" src/recommender/repositories/` → 0 筆
      - `grep -rn "getattr(settings" src/recommender/` → 0 筆
      - `grep -rn "NotImplementedError" src/recommender/services/` → 0 筆
- [x] **7.5** B2 實效驗證：設 `BEDROCK_GUARDRAIL_ID=gr-test` 啟動，確認 `AgentService._guardrail_config()` 回非 None dict（可在 6.x 補一個小單元測試取代手動驗）。
