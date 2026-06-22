# architecture-convergence — Tasks

> Ordering principle: the Python three-tier stack is built bottom-up (config/models/schemas → repositories → services → api → docs → tests → verification). After each phase the app should still start (`uvicorn recommender.main:app`), ensuring every change can be verified independently.
>
> ⚠️ This change **must not produce any new migration**. If any task reaches a point where it "seems to need a migration," stop and confirm with the user — that means the work has drifted out of the planned scope.

## Phase 1 — config / schemas (B2, C1)

- [x] **1.1 (B2)** `src/recommender/config.py`: add `bedrock_guardrail_id: str | None = None` and `bedrock_guardrail_version: str | None = None` to the Bedrock section of `Settings`.
      ✅ Acceptance: `python -c "from recommender.config import settings; print(settings.bedrock_guardrail_id)"` prints `None`; after setting `BEDROCK_GUARDRAIL_ID=test`, rerunning prints `test` (proving the env binding takes effect and is no longer swallowed by `extra="ignore"`).
- [x] **1.2 (C1)** Delete `src/recommender/schemas/canonical.py`; in `schemas/__init__.py` remove the canonical import at :2-6 and remove `CanonicalProduct` / `CanonicalCustomer` / `MergedDataset` from `__all__` (:22-24).
      ✅ Acceptance: `grep -rn "canonical\|CanonicalProduct\|MergedDataset" src/ --include="*.py"` leaves only the comment text at `dataset_service.py:35` (not an import); the app imports cleanly.

## Phase 2 — repositories (A1, B1)

- [x] **2.1 (A1)** `repositories/recommendation_repo.py`: add a required keyword argument `model_id: str` to `create_from_agent_output`; change the internal `model_id=settings.bedrock_model_id` (:36) to use the parameter; delete `from recommender.config import settings` (:5).
      ✅ Acceptance: `grep -n "settings" src/recommender/repositories/recommendation_repo.py` returns 0 hits. (At this point the call at `pipeline_service.py:84` will be missing an argument — it is supplied in task 3.3; fix the caller alongside this so no red state is left dangling before Phase 2 ends: see 3.3.)
- [x] **2.2 (B1)** `repositories/job_repo.py:42`: change `raise ValueError(...)` to `raise NotFoundError(...)`, adding `from recommender.errors import NotFoundError`.
      ✅ Acceptance: `grep -rn "raise ValueError" src/recommender/repositories/` returns 0 hits.

## Phase 3 — services (A1, A2, A3, B1, B2, C2)

- [x] **3.1 (A2/A3)** Add `src/recommender/services/recommendation_service.py`: `RecommendationService(rec_repo)`, `get(rec_id) -> RecommendationPublic` (raises `NotFoundError` when not found), `list_by_customer(customer_id, limit=20) -> list[RecommendationPublic]`. Convert via `RecommendationPublic.model_validate(orm)`. No interface / base class.
      ✅ Acceptance: the class has only a constructor + two read methods, with no superfluous abstraction.
- [x] **3.2 (A2/A3)** `services/evaluation_service.py`: add `get(eval_id) -> EvaluationPublic` and `list_by_recommendation(recommendation_id) -> list[EvaluationPublic]`; change `evaluate()` (:35) back to returning `EvaluationPublic` (wrapping the repo's returned ORM with `model_validate`).
      ✅ Acceptance: the return type annotations of `evaluate` / `get` / `list_by_recommendation` are all Pydantic DTOs, with no `-> Evaluation`.
- [x] **3.3 (A1/A3/B1)** `services/pipeline_service.py`:
      (a) inside `run()`, pass `model_id=settings.bedrock_model_id` to `create_from_agent_output(...)` (add the settings import);
      (b) move the `_to_response` logic from `api/pipelines.py:32-43` in as a private method, and change `create_job` / `get_job` back to returning `JobResponse`;
      (c) `get_job` raises `NotFoundError` when not found (instead of returning `None`).
      ✅ Acceptance: no method of `pipeline_service.py` returns a `PipelineJob` for use by the API layer (`run()`'s internal ORM operations don't count — it returns nothing to the API).
- [x] **3.4 (B2/C2)** `services/agent_service.py`:
      (a) change the two `getattr(settings, ...)` calls in `_guardrail_config()` (:85, :89) to read `settings.bedrock_guardrail_id` / `settings.bedrock_guardrail_version or "DRAFT"` directly;
      (b) delete `_select_prompt_variant` (:62-70) and the variant dead branch inside `analyze()` (:46-48); simplify `analyze()`'s return to `RecommendationOutput` (drop the tuple);
      (c) remove the `prompt_repo` parameter from `__init__` (zero usages after C2); update the docstring accordingly (drop the inaccurate description about "fetching the active prompt from the DB PromptVariant table").
      ✅ Acceptance: `grep -n "getattr(settings\|NotImplementedError\|prompt_repo" src/recommender/services/agent_service.py` returns 0 hits.
- [x] **3.5 (C2)** `services/pipeline_service.py:77`: in line with 3.4(b), adjust the `analyze()` unpacking (drop `prompt_variant_id`), and delete the dead TODO at :89-90.
      ✅ Acceptance: `grep -n "prompt_variant_id" src/recommender/services/pipeline_service.py` returns 0 hits.

## Phase 4 — API + DI (A2, A3, B1)

- [x] **4.1 (A2)** `deps.py`: add a `get_recommendation_service(rec_repo) -> RecommendationService` provider and `RecommendationServiceDep`; per 3.4(c), drop the `prompt_repo` injection from `get_agent_service` (keep `get_prompt_variant_repo` / `PromptVariantRepoDep` — the repo itself is not dead code, it is dormant infrastructure).
      ✅ Acceptance: the app starts with no DI errors; `deps.py` remains the single wiring point.
- [x] **4.2 (A2/B1)** `api/recommendations.py`: change both endpoints to inject `RecommendationServiceDep`; delete the `HTTPException` import and the manual 404 raises at :13-14.
      ✅ Acceptance: no `HTTPException` and no repo injection in the file.
- [x] **4.3 (A2/B1)** `api/evaluations.py`: change `get_evaluation` (:22) and `list_by_recommendation` (:30) to go through `EvaluationServiceDep`; delete the `HTTPException` import and the manual 404 raises at :25-26; delete the `EvaluationRepoDep` injection.
      ✅ Acceptance: no `HTTPException` and no repo injection in the file.
- [x] **4.4 (A3/B1)** `api/pipelines.py`: delete `_to_response` (:32-43) and `HTTPException` (:2, :27-28); `run_pipeline` schedules the background task using `job.job_id` (the service already returns `JobResponse`); `get_job` directly `return await service.get_job(job_id)`.
      ✅ Acceptance: `grep -rn "HTTPException" src/recommender/api/` returns 0 hits across the entire API layer.

## Phase 5 — docs (C2, C3, D1)

- [x] **5.1 (D1)** Sync the structure of `docs/architecture/architecture.md`: remove/annotate the decommissioned `SalesAnalysisService`, `api/analyses.py`, `/analyses/sales/*` (in the §2 directory tree, §5.5, the §7.x data flows, the endpoint list, the phase table at :610-637, etc.); add coverage of `chains/` (LCEL factory + `*_PROMPT_VERSION`), `llm.py` (`lru_cache` Bedrock builder + lifespan warm-up), `prompts.py` (`.md` loading + immutable version cache), `errors.py` (domain exceptions → main.py handler), `deps.py` (centralized DI), and `PromoForecastService` (451 lines, not yet wired to the API, with 33 hardcoded company tax IDs — its orphaned state).
      ✅ Acceptance: `grep -n "SalesAnalysisService\|analyses.py" docs/architecture/architecture.md` leaves only "decommissioned" annotation lines; the document's directory tree matches the actual output of `find src -name "*.py"`.
- [x] **5.2 (C2/C3)** In the same document's data-model section, add two annotations: (a) the `prompt_variant` table is dormant — the `.md` files are the sole runtime prompt source, the table is retained as future A/B infrastructure; (b) the 6 HubSpot columns of the `recommendation` table are Phase 4 reserved, with only `hubspot_sync_status=pending` currently having a write path.
      ✅ Acceptance: both annotations can be grep'd (`dormant`, `Phase 4 reserved`).

## Phase 6 — tests (D2)

- [x] **6.1** Create `tests/conftest.py`: set the `ANALYZER_MOCK_MODE=true` environment variable before any `recommender` import; provide an `httpx.AsyncClient(transport=ASGITransport(app))` fixture. State the precondition in the docstring: `docker compose up -d` (dev Postgres `localhost:5434`).
      ✅ Acceptance: `pytest --collect-only` collects successfully.
- [x] **6.2** `tests/test_pipeline_e2e.py` (mock mode, no Bedrock calls):
      (a) happy path: `POST /pipelines/run` → 202 + job_id → poll `GET /pipelines/{id}` until `done` → `GET /recommendations/{rec_id}` to verify the mock fixture fields → `POST /evaluations/{rec_id}` → 201 + `judge_model_id == "mock"` → `GET /evaluations/by-recommendation/{rec_id}` contains that record;
      (b) failure path (verifying the B1 chain): `GET /pipelines/999999`, `GET /recommendations/999999`, `GET /evaluations/999999` all return 404 + `detail`.
      ✅ Acceptance: the tests pass and `ANALYZER_MOCK_MODE=true` throughout (zero Bedrock calls, zero cost).
- [x] **6.3** `tests/test_etl_units.py`: pure functions of `promo_forecast_service` (`_filter_zhuanhu_dealers` / `_normalize_dealer_id` / `_classify_legal_categories` / `_build_reasoning` / `_rank_opportunities` / `opportunities_to_dataframe`) + the aggregation format of `evaluation_service._build_inputs`. All without DB / without network (⭐trade-off 4: units don't touch the DB).
      ✅ Acceptance: running `pytest tests/test_etl_units.py` standalone passes without docker.
- [x] **6.4** `tests/test_chains.py`: per ⭐trade-off 5 (finalized in design.md §7.4), test the chain-assembly contract — use a custom `FakeStructuredChatModel(GenericFakeChatModel)` (stub `bind_tools` to return `self.bind(...)`) feeding an `AIMessage` carrying `tool_calls` into `build_recommendation_chain` / `build_judge_chain`, verifying that the prompt variables are complete and the output types are correct (recommendation→`RecommendationOutput`; judge→`{"parsed", "raw"}` dict). **Do not** use `FakeListChatModel` directly (it has no `bind_tools` and won't pass `with_structured_output`). Test the service orchestration layer separately by injecting the whole chain via `RunnableLambda` (§7.4(a)).
      ✅ Acceptance: the tests pass with no Bedrock calls; they run standalone without docker.

## Phase 7 — Verification (after all items are complete)

- [x] **7.1** `pytest` all green (precondition: `docker compose up -d`).
- [x] **7.2** **Zero-migration verification**: record the `alembic current` output before starting, run it again after finishing — the revision must be identical; `git status alembic/versions/` shows no new files. If anyone wants to add a migration along the way → stop and confirm with the user.
- [x] **7.3** Manual API smoke test (mock mode, start uvicorn with `ANALYZER_MOCK_MODE=true`):
      - `POST /pipelines/run` (any customer_id/brand/month) → 202
      - `GET /pipelines/{job_id}` → `done` with `recommendation_id` non-null
      - `GET /recommendations/{rec_id}` → 200, `GET /recommendations/999999` → 404
      - `POST /evaluations/{rec_id}` → 201, `GET /evaluations/999999` → 404
      - response body fields are identical to before the change (contract unchanged)
- [x] **7.4** Full boundary grep suite (the acceptance greps in design.md §3):
      - `grep -rn "HTTPException" src/recommender/api/` → 0 hits
      - `grep -rn "raise ValueError" src/recommender/repositories/` → 0 hits
      - `grep -rn "from recommender.config import settings" src/recommender/repositories/` → 0 hits
      - `grep -rn "getattr(settings" src/recommender/` → 0 hits
      - `grep -rn "NotImplementedError" src/recommender/services/` → 0 hits
- [x] **7.5** B2 effectiveness check: start with `BEDROCK_GUARDRAIL_ID=gr-test` set and confirm `AgentService._guardrail_config()` returns a non-None dict (a small unit test added in 6.x may replace this manual check).
