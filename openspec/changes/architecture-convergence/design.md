# architecture-convergence — Design

## 0. 設計總綱

- **Simplicity First**：每項變更取「修正邊界違規所需的最小改動」。不引入 interface、抽象基類、generic repository 等新抽象。
- **行為不變**：本次是收斂不是改功能。所有 endpoint 的 HTTP contract（路徑、status code、response body 欄位）前後一致 —— 唯一例外是錯誤訊息來源從 router 內聯字串變成 `NotFoundError` 訊息，格式仍是 `{"detail": "..."}`（`main.py:65-68` handler 已如此輸出）。
- **零 migration**：不碰 DB schema。`PromptVariant` 表與 HubSpot 欄位原封不動。

## 1. 分層職責 — 修正前後對照

### 修正前（現況，三處滲漏）

```
api/recommendations.py ──直接──▶ RecommendationRepository   (繞過 service)
api/evaluations.py     ──直接──▶ EvaluationRepository       (繞過 service)
api/* 自行 raise HTTPException(404)                          (HTTP 知識散落)

PipelineService ──回傳──▶ PipelineJob (ORM) ──▶ api/pipelines.py:_to_response 轉
EvaluationService ──回傳──▶ Evaluation (ORM) ──▶ response_model 隱式轉

RecommendationRepository ──讀──▶ settings.bedrock_model_id   (repo 知道業務設定)
job_repo ──拋──▶ ValueError                                  (三套錯誤型別並存)
```

### 修正後（目標）

```
Layer 1  api/            只做：接 HTTP、schema 驗證、呼叫 service、回 DTO
                          不做：注入 repo、拋 HTTPException、ORM→DTO 轉換
   │ Depends(service)
   ▼
Layer 2  services/       只做：業務邏輯、編排、ORM→Pydantic DTO 轉換、讀 settings
                          查無資源 → 拋 NotFoundError
   │
   ▼
Layer 3  repositories/   只做：純 CRUD。所有業務參數（如 model_id）由 caller 傳入
                          查無資源(寫入路徑) → 拋 NotFoundError；read 路徑回 None

橫切     main.py          NotFoundError handler → 404；Exception handler → 500
         errors.py        domain 例外唯一定義處
         deps.py          DI wiring 唯一定義處
```

## 2. 受影響檔案清單

| 層 | 檔案 | 變更 | 對應項 |
|----|------|------|--------|
| config | `src/recommender/config.py` | `Settings` 新增 `bedrock_guardrail_id` / `bedrock_guardrail_version`（皆 `str \| None = None`） | B2 |
| schemas | `src/recommender/schemas/canonical.py` | **刪除** | C1 |
| schemas | `src/recommender/schemas/__init__.py` | 移除 canonical import（:2-6）與 `__all__` 三項（:22-24） | C1 |
| repositories | `src/recommender/repositories/recommendation_repo.py` | `create_from_agent_output` 新增必填 `model_id: str` 參數；移除 `from recommender.config import settings`（:5、:36） | A1 |
| repositories | `src/recommender/repositories/job_repo.py` | `update_status` 的 `ValueError`（:42）改 `NotFoundError` | B1 |
| services | `src/recommender/services/recommendation_service.py` | **新增** — 薄 read service | A2, A3 |
| services | `src/recommender/services/evaluation_service.py` | 補 `get` / `list_by_recommendation` read 方法；`evaluate` 改回 `EvaluationPublic`（:35） | A2, A3 |
| services | `src/recommender/services/pipeline_service.py` | `create_job` / `get_job` 改回 `JobResponse`；`get_job` 查無拋 `NotFoundError`（取代回 `None`）；`run()` 內呼叫 `create_from_agent_output` 補傳 `model_id`（:84）；`analyze()` 回傳值解包調整（:77） | A1, A3, B1 |
| services | `src/recommender/services/agent_service.py` | 刪 `_select_prompt_variant`（:62-70）與 `analyze()` 內 variant dead branch（:46-48）；`analyze()` 簽名簡化；`getattr` 改直讀 settings（:85, :89） | B2, C2 |
| api | `src/recommender/api/recommendations.py` | 改注入 `RecommendationService`；移除 `HTTPException` | A2, B1 |
| api | `src/recommender/api/evaluations.py` | read 端點改注入 `EvaluationService`；移除 `HTTPException`（:26） | A2, B1 |
| api | `src/recommender/api/pipelines.py` | 刪 `_to_response`（:32-43）；移除 `HTTPException`（:28）；直接回 service 給的 `JobResponse` | A3, B1 |
| DI | `src/recommender/deps.py` | 新增 `get_recommendation_service` provider + `RecommendationServiceDep` | A2 |
| docs | `docs/architecture/architecture.md` | 移除 SalesAnalysisService / analyses 殘留；補 `chains/` `llm.py` `prompts.py` `errors.py` `deps.py` `PromoForecastService`；標註 PromptVariant 未連通、HubSpot 欄位 Phase 4 reserved | C2, C3, D1 |
| tests | `tests/conftest.py`、`tests/test_pipeline_e2e.py`、`tests/test_etl_units.py`、`tests/test_chains.py` | **新增** | D2 |

不動的檔案：`models/*`（零 schema 變更）、`alembic/`、`prompts/`、`chains/`、`llm.py`、`prompts.py`、`errors.py`（`NotFoundError` 既有定義已夠用，不加新例外型別）。

## 3. 錯誤處理統一後的流

```
repository (寫入路徑查無前置資源，如 job_repo.update_status)
    └─ raise NotFoundError("Job {id} not found")
repository (read 路徑，如 rec_repo.get)
    └─ return None        ← repo 的 get 維持回 Optional，「查無是否為錯誤」是業務判斷，由 service 決定
service
    └─ if obj is None: raise NotFoundError(...)
api router
    └─ 不 try/except、不 raise HTTPException —— 例外直接往上飄
main.py:65 @app.exception_handler(NotFoundError)
    └─ JSONResponse(404, {"detail": str(exc)})
main.py:71 @app.exception_handler(Exception)
    └─ JSONResponse(500, {"detail": "Internal server error"})（traceback 只進 log）
```

**邊界 case**：`PipelineService.run()` 在 BackgroundTask 執行，不在 HTTP request 生命週期內 —— 它拋的 `NotFoundError` 不會經過 handler，由既有的 `logger.exception` + re-raise 模式處理（`pipeline_service.py:99-107`），維持現狀不動。

**驗收 grep**：完成後 `grep -rn "HTTPException" src/recommender/api/ --exclude=health.py` 應為 0 筆、`grep -rn "raise ValueError" src/recommender/repositories/` 應為 0 筆。
注意：`api/health.py` 的 `HTTPException(503)` **刻意保留** —— 那是 readiness probe 的基礎設施可用性回報（DB 連不上回 503），不是業務 `NotFoundError`，不走全域 handler。驗收 grep 因此排除 health.py。

## 4. 薄 read service — 方法簽名

不做 interface、不做基類，就是普通 class + 建構子注入 repo（沿用既有風格）：

```python
# src/recommender/services/recommendation_service.py（新增）
class RecommendationService:
    def __init__(self, rec_repo: RecommendationRepository) -> None: ...

    async def get(self, rec_id: int) -> RecommendationPublic:
        """查無 → raise NotFoundError"""

    async def list_by_customer(
        self, customer_id: str, limit: int = 20
    ) -> list[RecommendationPublic]: ...
```

```python
# src/recommender/services/evaluation_service.py（既有 class 補方法，不新增 class）
class EvaluationService:
    async def evaluate(self, recommendation_id: int) -> EvaluationPublic: ...  # 改回傳型別

    async def get(self, eval_id: int) -> EvaluationPublic:
        """查無 → raise NotFoundError"""

    async def list_by_recommendation(
        self, recommendation_id: int
    ) -> list[EvaluationPublic]: ...
```

```python
# src/recommender/services/pipeline_service.py（既有方法改回傳型別）
class PipelineService:
    async def create_job(self, customer_id: str, brand: str, month: str) -> JobResponse: ...
    async def get_job(self, job_id: int) -> JobResponse:
        """查無 → raise NotFoundError（取代現在回 None 由 api 拋 404）"""
    # ORM→JobResponse 轉換：私有 _to_response(job: PipelineJob) -> JobResponse
    # （邏輯自 api/pipelines.py:32-43 原樣搬入，欄位名 job_id≠id 故不能用 model_validate）
```

DTO 轉換規則：`RecommendationPublic` / `EvaluationPublic` 已設 `from_attributes=True`（`schemas/public.py:16,32`），service 用 `XxxPublic.model_validate(orm_obj)` 一行轉；`JobResponse` 欄位名與 ORM 不同（`job_id` vs `id`），沿用顯式建構。

`api/pipelines.py:20` 的 `background.add_task(service.run, job.id)` 改用 `job.job_id`（`JobResponse` 欄位名）。

## 5. Settings 新欄位（B2）

```python
# config.py — Bedrock 段新增
bedrock_guardrail_id: str | None = None       # 未設 → guardrail 不啟用（與現行為一致）
bedrock_guardrail_version: str | None = None  # 未設且 id 有設 → agent_service fallback "DRAFT"
```

`agent_service._guardrail_config()` 改為：

```python
if not settings.bedrock_guardrail_id:
    return None
return {
    "guardrailIdentifier": settings.bedrock_guardrail_id,
    "guardrailVersion": settings.bedrock_guardrail_version or "DRAFT",
    "trace": "enabled",
}
```

行為差異：以前 env 設了 `BEDROCK_GUARDRAIL_ID` 會被 `extra="ignore"`（`config.py:12`）吞掉、guardrail 靜默失效；改完後 pydantic-settings 正常綁定，guardrail 真的會進 `additional_model_request_fields`。`extra="ignore"` 本身保留（其他未知 env var 仍應忽略），修法是「把要用的欄位宣告出來」而非改 extra 策略。

## 6. Prompt 單一來源後的載入流（C2）

```
唯一 runtime 來源：prompts/{module}/{version}.md
    │  chains/recommendation.py:19  RECOMMENDATION_PROMPT_VERSION = "recommendation/v1.0"
    │  chains/judge.py:15           JUDGE_PROMPT_VERSION = "judge/v1.0"
    ▼
prompts.py  load_system_prompt(version, human_template)
    └─ @cache 讀檔（version 即 cache key，prompt immutable、改內容須發新版號）
    ▼
chains/build_*_chain(llm) → prompt | llm.with_structured_output(...)
```

移除項：`agent_service._select_prompt_variant`（:62-70，`NotImplementedError` 死路）、`analyze()` 內的 variant 註解分支（:46-48）。`analyze()` 回傳簡化為 `RecommendationOutput`（原本 tuple 第二元素 `variant_id` 永遠 `None`），`pipeline_service.py:77` 解包與 :89-90 的 dead TODO 一併清掉。

保留項（forward-only）：`PromptVariant` 表、`prompt_variant_repo.py`、`deps.py` 的 `PromptVariantRepoDep`、`models/recommendation.py:42` 的 `prompt_variant_id` FK 欄位。`AgentService.__init__` 的 `prompt_repo` 參數一併保留或移除？**決策：移除**（`AgentService` 在 C2 後不再有任何 `prompt_repo` 使用點，留著是新的死碼；repo class 本身保留，未來 A/B 實作時再從 `deps.py` 重新注入，wiring 改回來只要兩行）。`deps.py:61-62` 的 `get_agent_service` 同步調整。

文件標註（進 architecture.md 資料模型段）：`prompt_variant` 表為 dormant —— schema 已就緒、無 runtime 讀寫路徑，為未來 prompt A/B 預留。

## 7. 測試策略（D2）

### 7.1 目錄與基建

```
tests/
├── conftest.py            # env 固定 ANALYZER_MOCK_MODE=true、app fixture、async client
├── test_pipeline_e2e.py   # mock-mode 全流程（pipeline → recommendation → evaluation）
├── test_etl_units.py      # ETL 純函式單元測試（無 DB、無網路）
└── test_chains.py         # chain 組裝測試（fake LLM 注入，無 Bedrock）
```

`pyproject.toml` 已備好 `testpaths=["tests"]`、`asyncio_mode="auto"`、pytest>=8.3、pytest-asyncio>=0.25（:46-47, :67-69），不需動設定。HTTP 測試走 `httpx.AsyncClient` + `ASGITransport`（httpx 已是 FastAPI 相依，若 lock 檔未含則加入 dev dependency —— 這是本次唯一允許的依賴變動）。

### 7.2 mock_mode e2e

- conftest 在 import app **之前** 設 `ANALYZER_MOCK_MODE=true`（`Settings` 在 module import 時實例化，`config.py:50`）。
- DB 用 docker-compose dev Postgres（`localhost:5434`，既有開發環境，不引入新依賴）；測試前置條件 = `docker compose up -d postgres`，寫進 conftest docstring 與 tasks 驗收。**⭐取捨 4：刻意不用 in-memory SQLite** —— 本專案 JSON 欄位（未來 JSONB）與 `Datetime(timezone=True)` 在 SQLite 下語意會靜默失準（pytest-mock-resources 文件），e2e 必須對真實 Postgres 跑。testcontainers 是更佳的隔離方案（每次 fresh 容器、不污染 dev DB），但專案已有 docker-compose Postgres，Simplicity First 下先複用，testcontainers 列為未來升級而非本次引入。
- **分層**：ETL 純函式測試（§7.3）與 chain 組裝測試（§7.4）**完全不碰 DB**，可不起 docker 單獨跑（`pytest tests/test_etl_units.py tests/test_chains.py`）；只有 e2e（§7.2）需要 Postgres。
- `DatasetService.prepare` 目前是 stub（不真打 S3，`dataset_service.py:41-43`），故 e2e 不需 LocalStack。
- 流程斷言：`POST /pipelines/run` 回 202 + `job_id` → 輪詢 `GET /pipelines/{job_id}` 至 `status=done` → `GET /recommendations/{recommendation_id}` 回 mock fixture 欄位 → `POST /evaluations/{rec_id}` 回 201 + mock 分數（`judge_model_id="mock"`）→ `GET /evaluations/by-recommendation/{rec_id}` 含該筆。
- 負路徑斷言（驗 B1）：`GET /pipelines/999999`、`GET /recommendations/999999`、`GET /evaluations/999999` 皆回 404 + `{"detail": ...}`，證明 NotFoundError → handler 鏈通。

### 7.3 ETL 純函式單元測試

純函式、無 IO，直接餵小型 DataFrame / Pydantic fixture：

- `promo_forecast_service.py`：`_filter_zhuanhu_dealers`（:216）、`_normalize_dealer_id`（:226）、`_classify_legal_categories`（:234）、`_build_reasoning`（:297）、`_rank_opportunities`（:350）、`opportunities_to_dataframe`（:429）。
- `evaluation_service._build_inputs`（:91）：驗證 `products_text` 聚合格式、分數 formatting（對齊「ETL First, LLM Last」—— 這段聚合正是不讓 LLM 算數的防線，值得測）。

### 7.4 Chain 測試（fake LLM 注入）— ⭐取捨 5 定稿

**根因（langchain-core 0.3.x 原始碼確認）**：`FakeListChatModel` / `FakeChatModel` / `GenericFakeChatModel` 三個 fake model **都沒實作 `bind_tools`**，而 `llm.with_structured_output(...)` 底層就是走 `bind_tools`（tool calling）。所以「直接把 fake llm 傳進 `build_*_chain()`」會在 chain 組裝或 invoke 時失敗。這是 LangChain 生態當前的測試技術債，非本專案設計問題。對策分兩段，依「你要測什麼」選用：

**(a) 測 service 編排邏輯（pipeline_service / evaluation_service 怎麼用 chain）→ 注入 `RunnableLambda` 當整條 chain，完全繞過 fake model**

```python
from langchain_core.runnables import RunnableLambda
from recommender.schemas.recommendation import RecommendationOutput

fake_chain = RunnableLambda(lambda _: RecommendationOutput(customer_id="DEALER_001", items=[...]))
# 注入點：build_*_chain 的回傳值（service 持有的 chain），而非 llm
```
優點：回真正的 Pydantic object、下游斷言乾淨、`RunnableLambda` 是官方可組合元件、不碰 `bind_tools`。這是**多數測試的首選**。

**(b) 測 chain 本身的組裝 contract（prompt 變數、structured output 型別）→ 自製 `FakeStructuredChatModel` stub `bind_tools`**

```python
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages.tool import ToolCall

class FakeStructuredChatModel(GenericFakeChatModel):
    """GenericFakeChatModel + stub bind_tools，讓 with_structured_output 可用。"""
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=tools, tool_choice=tool_choice)

fake_llm = FakeStructuredChatModel(messages=iter([
    AIMessage(content="", tool_calls=[ToolCall(name="RecommendationOutput", args={...}, id="call_1")]),
]))
chain = build_recommendation_chain(fake_llm)
result = chain.invoke({"customer_id": "DEALER_001", "dataset_s3_key": "s3://b/k"})
assert isinstance(result, RecommendationOutput)
```
judge chain 用 `include_raw=True`，斷言其回傳為 `{"parsed": ..., "raw": ...}` dict。

斷言重點不是 LLM 輸出內容，而是 **chain 組裝 contract**：prompt 變數齊備（recommendation 的 `customer_id` / `dataset_s3_key`；judge 的注入變數）、輸出型別正確。

依據：[langchain-core fake_chat_models.py 原始碼](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/fake_chat_models.py)、[LangChain 官方 Unit Testing](https://docs.langchain.com/oss/python/langchain/test/unit-testing)、GitHub Issue #36349 `bind_tools` stub workaround。langchain-core ≥ 0.3。

### 7.5 不測什麼

- 不打真 Bedrock（safety.md：花錢動作）、不測 LLM 輸出品質（那是 evaluation 模組的 runtime 職責）。
- 不為 `S3Service` / `DatasetService` stub 補測試 —— stub 行為會在真 ETL 實作時整個換掉，現在測是浪費。

## 8. 關鍵設計取捨（已依最佳實踐研究裁決）

> 下表 5 個標 ⭐ 的取捨經 SOLID 原則 + LangChain/FastAPI 官方文件 + GitHub 真實專案研究後定稿（研究報告見對話紀錄）。其餘為 §0 Simplicity First 推導。

| 取捨 | 決定 | 依據（出處 / 原則） | 信心 |
|------|------|------|------|
| ⭐1 repo 的 `get` 回 `None` 還是拋 NotFoundError | **回 `None`，service 判斷後拋** | full-stack-fastapi-template 的 crud 全回 `T\|None`；FastAPI 官方 *Handling Errors* 在上層判空；SRP：repo 不懂 HTTP 語意。寫入路徑前置查找失敗（`update_status`）例外，那是資料完整性問題 repo 直接拋 | 高 |
| ⭐2 `AgentService` 的 `prompt_repo` 參數 | **移除**（repo class + deps provider 保留為 dormant） | YAGNI（Fowler）+ SOLID ISP：不強迫 client 依賴用不到的東西；DI 最佳實踐視多餘 constructor 參數為 red flag。恢復 A/B 時重新注入只要兩行 | 高 |
| ⭐3 A1 的 `model_id` 由誰提供、mock 記什麼 | **`PipelineService.run()` 傳 `settings.bedrock_model_id`；mock mode 維持原值不改 `"mock"`** | Fowler refactoring 鐵律：不改 observable behavior（連修 bug 都不算重構）。要改 mock 值另開 `fix:` commit | 高 |
| ⭐4 測試 DB | **分層：unit（ETL 純函式 / chain）不碰 DB；e2e/repo 整合用 docker-compose dev Postgres（`localhost:5434`），不用 SQLite** | SQLModel 官方測試教學雖用 SQLite，但本專案 JSON 欄位 + 未來 JSONB 語意在 SQLite 會失準（pytest-mock-resources 文件）。testcontainers 是更佳隔離方案，但專案已有 docker-compose Postgres，Simplicity First 下先複用、testcontainers 列為未來升級 | 高 |
| ⭐5 chain fake LLM 注入法 | **兩段式：測 service 邏輯 → 注入 `RunnableLambda` 當整條 chain；測 chain 組裝 → 自製 `FakeStructuredChatModel(GenericFakeChatModel)` stub `bind_tools`**（詳見 §7.4） | langchain-core 0.3.x 原始碼確認三個 fake model 均無 `bind_tools`，而 `with_structured_output` 底層走 `bind_tools`；官方 *unit-testing* 文件 + GitHub Issue #36349 workaround | 高 |
| read service 要不要 interface / 基類 | 不要 | YAGNI。三層架構的價值在「層的職責」不在「層的抽象」；POC 規模下抽象只增加跳轉成本 | — |
| `EvaluationService` read 方法另開 class？ | 不另開，補進既有 class | 一個 module 一個 service，read/write 同屬 evaluation 業務 | — |
| guardrail version 預設 | Settings 預設 `None`、使用端 fallback `"DRAFT"` | 與現行 `getattr(..., "DRAFT")` 行為一致；不把 AWS 特定魔術字串放進全域 Settings 預設值 | — |

**出處清單**：[full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template) · [FastAPI Handling Errors](https://fastapi.tiangolo.com/tutorial/handling-errors/) · [Fowler Refactoring](https://refactoring.com/) · [SQLModel Testing](https://sqlmodel.tiangolo.com/tutorial/fastapi/tests/) · [pytest-mock-resources SQLite](https://pytest-mock-resources.readthedocs.io/en/latest/sqlite.html) · [langchain-core fake_chat_models.py](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/fake_chat_models.py) · [LangChain Unit Testing](https://docs.langchain.com/oss/python/langchain/test/unit-testing)
