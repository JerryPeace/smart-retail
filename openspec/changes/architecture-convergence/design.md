# architecture-convergence — Design

## 0. Design overview

- **Simplicity First**: each change takes "the minimal change needed to fix the boundary violation." Do not introduce new abstractions such as interfaces, abstract base classes, or generic repositories.
- **Behavior unchanged**: this time is convergence, not a functional change. The HTTP contract of every endpoint (path, status code, response body fields) stays identical before and after — the only exception is that the error message source changes from an inline string in the router to the `NotFoundError` message, with the format still `{"detail": "..."}` (the `main.py:65-68` handler already outputs this).
- **Zero migration**: do not touch the DB schema. The `PromptVariant` table and the HubSpot columns stay exactly as they are.

## 1. Layer responsibilities — before/after comparison

### Before (current state, three leaks)

```
api/recommendations.py ──direct──▶ RecommendationRepository   (bypasses service)
api/evaluations.py     ──direct──▶ EvaluationRepository       (bypasses service)
api/* raise HTTPException(404) themselves                     (HTTP knowledge scattered)

PipelineService ──returns──▶ PipelineJob (ORM) ──▶ api/pipelines.py:_to_response converts
EvaluationService ──returns──▶ Evaluation (ORM) ──▶ response_model implicit conversion

RecommendationRepository ──reads──▶ settings.bedrock_model_id   (repo knows business config)
job_repo ──raises──▶ ValueError                                  (three error types coexist)
```

### After (target)

```
Layer 1  api/            does only: accept HTTP, schema validation, call service, return DTO
                          does not: inject repo, raise HTTPException, ORM→DTO conversion
   │ Depends(service)
   ▼
Layer 2  services/       does only: business logic, orchestration, ORM→Pydantic DTO conversion, read settings
                          resource not found → raise NotFoundError
   │
   ▼
Layer 3  repositories/   does only: pure CRUD. All business parameters (e.g. model_id) passed in by caller
                          resource not found (write path) → raise NotFoundError; read path returns None

cross-cutting  main.py    NotFoundError handler → 404; Exception handler → 500
               errors.py  the sole definition site of domain exceptions
               deps.py    the sole definition site of DI wiring
```

## 2. Affected file list

| Layer | File | Change | Item |
|----|------|------|--------|
| config | `src/recommender/config.py` | `Settings` adds `bedrock_guardrail_id` / `bedrock_guardrail_version` (both `str \| None = None`) | B2 |
| schemas | `src/recommender/schemas/canonical.py` | **delete** | C1 |
| schemas | `src/recommender/schemas/__init__.py` | remove the canonical import (:2-6) and the three `__all__` entries (:22-24) | C1 |
| repositories | `src/recommender/repositories/recommendation_repo.py` | `create_from_agent_output` adds required `model_id: str` parameter; remove `from recommender.config import settings` (:5, :36) | A1 |
| repositories | `src/recommender/repositories/job_repo.py` | change `update_status`'s `ValueError` (:42) to `NotFoundError` | B1 |
| services | `src/recommender/services/recommendation_service.py` | **new** — thin read service | A2, A3 |
| services | `src/recommender/services/evaluation_service.py` | add `get` / `list_by_recommendation` read methods; `evaluate` changed to return `EvaluationPublic` (:35) | A2, A3 |
| services | `src/recommender/services/pipeline_service.py` | `create_job` / `get_job` changed to return `JobResponse`; `get_job` raises `NotFoundError` on not found (replacing returning `None`); the call to `create_from_agent_output` in `run()` adds `model_id` (:84); adjust `analyze()`'s return value unpacking (:77) | A1, A3, B1 |
| services | `src/recommender/services/agent_service.py` | delete `_select_prompt_variant` (:62-70) and the variant dead branch in `analyze()` (:46-48); simplify `analyze()` signature; change `getattr` to direct settings read (:85, :89) | B2, C2 |
| api | `src/recommender/api/recommendations.py` | switch to injecting `RecommendationService`; remove `HTTPException` | A2, B1 |
| api | `src/recommender/api/evaluations.py` | read endpoints switch to injecting `EvaluationService`; remove `HTTPException` (:26) | A2, B1 |
| api | `src/recommender/api/pipelines.py` | delete `_to_response` (:32-43); remove `HTTPException` (:28); directly return the `JobResponse` given by the service | A3, B1 |
| DI | `src/recommender/deps.py` | add `get_recommendation_service` provider + `RecommendationServiceDep` | A2 |
| docs | `docs/architecture/architecture.md` | remove SalesAnalysisService / analyses remnants; document `chains/` `llm.py` `prompts.py` `errors.py` `deps.py` `PromoForecastService`; annotate PromptVariant as not wired up and HubSpot columns as Phase 4 reserved | C2, C3, D1 |
| tests | `tests/conftest.py`, `tests/test_pipeline_e2e.py`, `tests/test_etl_units.py`, `tests/test_chains.py` | **new** | D2 |

Files left untouched: `models/*` (zero schema changes), `alembic/`, `prompts/`, `chains/`, `llm.py`, `prompts.py`, `errors.py` (the existing `NotFoundError` definition is sufficient, no new exception type added).

## 3. Flow after unifying error handling

```
repository (write path, prerequisite resource not found, e.g. job_repo.update_status)
    └─ raise NotFoundError("Job {id} not found")
repository (read path, e.g. rec_repo.get)
    └─ return None        ← the repo's get keeps returning Optional; "whether not-found is an error" is a business judgment, decided by the service
service
    └─ if obj is None: raise NotFoundError(...)
api router
    └─ no try/except, no raise HTTPException — the exception simply floats up
main.py:65 @app.exception_handler(NotFoundError)
    └─ JSONResponse(404, {"detail": str(exc)})
main.py:71 @app.exception_handler(Exception)
    └─ JSONResponse(500, {"detail": "Internal server error"}) (traceback goes only to the log)
```

**Edge case**: `PipelineService.run()` executes in a BackgroundTask, outside the HTTP request lifecycle — the `NotFoundError` it raises does not pass through the handler and is handled by the existing `logger.exception` + re-raise pattern (`pipeline_service.py:99-107`), kept as-is.

**Acceptance grep**: after completion, `grep -rn "HTTPException" src/recommender/api/ --exclude=health.py` should yield 0 results, and `grep -rn "raise ValueError" src/recommender/repositories/` should yield 0 results.
Note: the `HTTPException(503)` in `api/health.py` is **intentionally kept** — that is the readiness probe's report of infrastructure availability (returns 503 when the DB is unreachable), not a business `NotFoundError`, and does not go through the global handler. The acceptance grep therefore excludes health.py.

## 4. Thin read service — method signatures

No interface, no base class — just a plain class + constructor injection of the repo (following the existing style):

```python
# src/recommender/services/recommendation_service.py (new)
class RecommendationService:
    def __init__(self, rec_repo: RecommendationRepository) -> None: ...

    async def get(self, rec_id: int) -> RecommendationPublic:
        """not found → raise NotFoundError"""

    async def list_by_customer(
        self, customer_id: str, limit: int = 20
    ) -> list[RecommendationPublic]: ...
```

```python
# src/recommender/services/evaluation_service.py (add methods to existing class, no new class)
class EvaluationService:
    async def evaluate(self, recommendation_id: int) -> EvaluationPublic: ...  # changed return type

    async def get(self, eval_id: int) -> EvaluationPublic:
        """not found → raise NotFoundError"""

    async def list_by_recommendation(
        self, recommendation_id: int
    ) -> list[EvaluationPublic]: ...
```

```python
# src/recommender/services/pipeline_service.py (existing methods change return type)
class PipelineService:
    async def create_job(self, customer_id: str, brand: str, month: str) -> JobResponse: ...
    async def get_job(self, job_id: int) -> JobResponse:
        """not found → raise NotFoundError (replacing the current return None with the api raising 404)"""
    # ORM→JobResponse conversion: private _to_response(job: PipelineJob) -> JobResponse
    # (moved verbatim from api/pipelines.py:32-43; field name job_id≠id so model_validate cannot be used)
```

DTO conversion rule: `RecommendationPublic` / `EvaluationPublic` already set `from_attributes=True` (`schemas/public.py:16,32`), so the service converts in one line with `XxxPublic.model_validate(orm_obj)`; `JobResponse`'s field names differ from the ORM (`job_id` vs `id`), so it stays explicit construction.

`background.add_task(service.run, job.id)` at `api/pipelines.py:20` changes to use `job.job_id` (the `JobResponse` field name).

## 5. New Settings fields (B2)

```python
# config.py — added to the Bedrock section
bedrock_guardrail_id: str | None = None       # unset → guardrail disabled (consistent with current behavior)
bedrock_guardrail_version: str | None = None  # unset but id set → agent_service falls back to "DRAFT"
```

`agent_service._guardrail_config()` changes to:

```python
if not settings.bedrock_guardrail_id:
    return None
return {
    "guardrailIdentifier": settings.bedrock_guardrail_id,
    "guardrailVersion": settings.bedrock_guardrail_version or "DRAFT",
    "trace": "enabled",
}
```

Behavioral difference: previously, setting `BEDROCK_GUARDRAIL_ID` in the env would be swallowed by `extra="ignore"` (`config.py:12`) and the guardrail would silently fail; after the fix, pydantic-settings binds it normally and the guardrail really does enter `additional_model_request_fields`. `extra="ignore"` itself is kept (other unknown env vars should still be ignored); the fix is "declare the fields we use" rather than changing the extra policy.

## 6. Prompt loading flow after a single source (C2)

```
sole runtime source: prompts/{module}/{version}.md
    │  chains/recommendation.py:19  RECOMMENDATION_PROMPT_VERSION = "recommendation/v1.0"
    │  chains/judge.py:15           JUDGE_PROMPT_VERSION = "judge/v1.0"
    ▼
prompts.py  load_system_prompt(version, human_template)
    └─ @cache file read (version is the cache key; prompt is immutable, content changes require a new version number)
    ▼
chains/build_*_chain(llm) → prompt | llm.with_structured_output(...)
```

Removed: `agent_service._select_prompt_variant` (:62-70, the `NotImplementedError` dead end) and the variant comment branch inside `analyze()` (:46-48). `analyze()`'s return is simplified to `RecommendationOutput` (the original tuple's second element `variant_id` was always `None`); the unpacking at `pipeline_service.py:77` and the dead TODO at :89-90 are cleaned up together.

Kept (forward-only): the `PromptVariant` table, `prompt_variant_repo.py`, the `PromptVariantRepoDep` in `deps.py`, and the `prompt_variant_id` FK field at `models/recommendation.py:42`. Keep or remove the `prompt_repo` parameter of `AgentService.__init__`? **Decision: remove** (after C2, `AgentService` has no remaining use of `prompt_repo`, so keeping it is new dead code; the repo class itself is kept, to be re-injected from `deps.py` when A/B is implemented in the future — rewiring is just two lines). The `get_agent_service` at `deps.py:61-62` is adjusted accordingly.

Doc annotation (goes into the architecture.md data model section): the `prompt_variant` table is dormant — schema is ready, no runtime read/write path, reserved for future prompt A/B.

## 7. Test strategy (D2)

### 7.1 Directory and infrastructure

```
tests/
├── conftest.py            # fix env ANALYZER_MOCK_MODE=true, app fixture, async client
├── test_pipeline_e2e.py   # mock-mode full flow (pipeline → recommendation → evaluation)
├── test_etl_units.py      # ETL pure-function unit tests (no DB, no network)
└── test_chains.py         # chain assembly tests (fake LLM injection, no Bedrock)
```

`pyproject.toml` already has `testpaths=["tests"]`, `asyncio_mode="auto"`, pytest>=8.3, pytest-asyncio>=0.25 ready (:46-47, :67-69), no config change needed. HTTP tests use `httpx.AsyncClient` + `ASGITransport` (httpx is already a FastAPI dependency; if the lock file doesn't include it, add it as a dev dependency — this is the only dependency change allowed this time).

### 7.2 mock_mode e2e

- conftest sets `ANALYZER_MOCK_MODE=true` **before** importing app (`Settings` is instantiated at module import time, `config.py:50`).
- The DB uses the docker-compose dev Postgres (`localhost:5434`, the existing dev environment, no new dependency introduced); the test prerequisite = `docker compose up -d postgres`, written into the conftest docstring and the tasks acceptance criteria. **⭐Trade-off 4: deliberately not using in-memory SQLite** — this project's JSON columns (future JSONB) and `Datetime(timezone=True)` would silently lose semantic fidelity under SQLite (pytest-mock-resources docs), so e2e must run against real Postgres. testcontainers is a better isolation option (a fresh container each time, no polluting the dev DB), but the project already has docker-compose Postgres, so under Simplicity First we reuse it first; testcontainers is listed as a future upgrade rather than introduced this time.
- **Layering**: the ETL pure-function tests (§7.3) and chain assembly tests (§7.4) **do not touch the DB at all** and can run standalone without starting docker (`pytest tests/test_etl_units.py tests/test_chains.py`); only e2e (§7.2) needs Postgres.
- `DatasetService.prepare` is currently a stub (does not actually hit S3, `dataset_service.py:41-43`), so e2e does not need LocalStack.
- Flow assertions: `POST /pipelines/run` returns 202 + `job_id` → poll `GET /pipelines/{job_id}` until `status=done` → `GET /recommendations/{recommendation_id}` returns the mock fixture fields → `POST /evaluations/{rec_id}` returns 201 + a mock score (`judge_model_id="mock"`) → `GET /evaluations/by-recommendation/{rec_id}` includes that record.
- Negative-path assertions (verifying B1): `GET /pipelines/999999`, `GET /recommendations/999999`, `GET /evaluations/999999` all return 404 + `{"detail": ...}`, proving the NotFoundError → handler chain is connected.

### 7.3 ETL pure-function unit tests

Pure functions, no IO, fed small DataFrame / Pydantic fixtures directly:

- `promo_forecast_service.py`: `_filter_zhuanhu_dealers` (:216), `_normalize_dealer_id` (:226), `_classify_legal_categories` (:234), `_build_reasoning` (:297), `_rank_opportunities` (:350), `opportunities_to_dataframe` (:429).
- `evaluation_service._build_inputs` (:91): verify the `products_text` aggregation format and score formatting (aligned with "ETL First, LLM Last" — this aggregation is precisely the line of defense against letting the LLM do arithmetic, worth testing).

### 7.4 Chain tests (fake LLM injection) — ⭐Trade-off 5 finalized

**Root cause (confirmed in langchain-core 0.3.x source)**: the three fake models `FakeListChatModel` / `FakeChatModel` / `GenericFakeChatModel` **all do not implement `bind_tools`**, while `llm.with_structured_output(...)` is built on top of `bind_tools` (tool calling). So "passing a fake llm directly into `build_*_chain()`" will fail during chain assembly or invoke. This is the current testing tech debt of the LangChain ecosystem, not a design problem of this project. The countermeasure has two parts, chosen by "what you want to test":

**(a) To test service orchestration logic (how pipeline_service / evaluation_service use the chain) → inject a `RunnableLambda` as the entire chain, bypassing the fake model completely**

```python
from langchain_core.runnables import RunnableLambda
from recommender.schemas.recommendation import RecommendationOutput

fake_chain = RunnableLambda(lambda _: RecommendationOutput(customer_id="DEALER_001", items=[...]))
# injection point: the return value of build_*_chain (the chain held by the service), not the llm
```
Advantages: returns a real Pydantic object, clean downstream assertions, `RunnableLambda` is an official composable component, and it does not touch `bind_tools`. This is the **first choice for most tests**.

**(b) To test the chain's own assembly contract (prompt variables, structured output type) → make a custom `FakeStructuredChatModel` that stubs `bind_tools`**

```python
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages.tool import ToolCall

class FakeStructuredChatModel(GenericFakeChatModel):
    """GenericFakeChatModel + stubbed bind_tools, so with_structured_output works."""
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=tools, tool_choice=tool_choice)

fake_llm = FakeStructuredChatModel(messages=iter([
    AIMessage(content="", tool_calls=[ToolCall(name="RecommendationOutput", args={...}, id="call_1")]),
]))
chain = build_recommendation_chain(fake_llm)
result = chain.invoke({"customer_id": "DEALER_001", "dataset_s3_key": "s3://b/k"})
assert isinstance(result, RecommendationOutput)
```
The judge chain uses `include_raw=True`; assert its return is a `{"parsed": ..., "raw": ...}` dict.

The assertion focus is not the LLM output content but the **chain assembly contract**: the prompt variables are complete (recommendation's `customer_id` / `dataset_s3_key`; judge's injected variables), and the output type is correct.

References: [langchain-core fake_chat_models.py source](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/fake_chat_models.py), [LangChain official Unit Testing](https://docs.langchain.com/oss/python/langchain/test/unit-testing), GitHub Issue #36349 `bind_tools` stub workaround. langchain-core ≥ 0.3.

### 7.5 What is not tested

- Do not hit real Bedrock (safety.md: costs money), do not test LLM output quality (that is the evaluation module's runtime responsibility).
- Do not add stub tests for `S3Service` / `DatasetService` — the stub behavior will be entirely replaced when the real ETL is implemented, so testing it now is wasteful.

## 8. Key design trade-offs (decided per best-practice research)

> The 5 trade-offs marked ⭐ below were finalized after research into SOLID principles + LangChain/FastAPI official docs + real GitHub projects (research report in the conversation history). The rest are derived from §0 Simplicity First.

| Trade-off | Decision | Basis (source / principle) | Confidence |
|------|------|------|------|
| ⭐1 should the repo's `get` return `None` or raise NotFoundError | **return `None`, the service judges and raises** | full-stack-fastapi-template's crud all return `T\|None`; FastAPI official *Handling Errors* checks for empties in the upper layer; SRP: the repo does not understand HTTP semantics. The write-path prerequisite lookup failure (`update_status`) is the exception — that is a data-integrity issue the repo raises directly | high |
| ⭐2 the `prompt_repo` parameter of `AgentService` | **remove** (repo class + deps provider kept as dormant) | YAGNI (Fowler) + SOLID ISP: do not force the client to depend on something it doesn't use; DI best practices treat a superfluous constructor parameter as a red flag. Re-injecting when restoring A/B is just two lines | high |
| ⭐3 who provides A1's `model_id`, and what the mock records | **`PipelineService.run()` passes `settings.bedrock_model_id`; mock mode keeps the original value `"mock"` unchanged** | Fowler's refactoring iron rule: do not change observable behavior (even fixing a bug doesn't count as refactoring). Changing the mock value is a separate `fix:` commit | high |
| ⭐4 test DB | **layered: unit (ETL pure functions / chain) does not touch the DB; e2e/repo integration uses the docker-compose dev Postgres (`localhost:5434`), not SQLite** | The SQLModel official testing tutorial uses SQLite, but this project's JSON columns + future JSONB semantics would lose fidelity under SQLite (pytest-mock-resources docs). testcontainers is a better isolation option, but the project already has docker-compose Postgres, so under Simplicity First we reuse it first and list testcontainers as a future upgrade | high |
| ⭐5 chain fake LLM injection method | **two-tier: to test service logic → inject `RunnableLambda` as the entire chain; to test chain assembly → make a custom `FakeStructuredChatModel(GenericFakeChatModel)` stubbing `bind_tools`** (see §7.4) | langchain-core 0.3.x source confirms all three fake models lack `bind_tools`, while `with_structured_output` is built on `bind_tools`; official *unit-testing* docs + GitHub Issue #36349 workaround | high |
| should the read service have an interface / base class | no | YAGNI. The value of the three-layer architecture is in "the responsibility of each layer," not "the abstraction of each layer"; at POC scale, abstraction only adds navigation cost | — |
| open a separate class for `EvaluationService` read methods? | no separate class, add to the existing class | one module one service; read/write both belong to the evaluation business | — |
| guardrail version default | Settings default `None`, caller falls back to `"DRAFT"` | consistent with the current `getattr(..., "DRAFT")` behavior; does not put an AWS-specific magic string into the global Settings default | — |

**Source list**: [full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template) · [FastAPI Handling Errors](https://fastapi.tiangolo.com/tutorial/handling-errors/) · [Fowler Refactoring](https://refactoring.com/) · [SQLModel Testing](https://sqlmodel.tiangolo.com/tutorial/fastapi/tests/) · [pytest-mock-resources SQLite](https://pytest-mock-resources.readthedocs.io/en/latest/sqlite.html) · [langchain-core fake_chat_models.py](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/fake_chat_models.py) · [LangChain Unit Testing](https://docs.langchain.com/oss/python/langchain/test/unit-testing)
