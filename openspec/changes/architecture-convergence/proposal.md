# architecture-convergence

## Why

After the cleaner→recommender refactor (commit 8fc336c), the FastAPI + LangChain main chain is up and running, but it left behind four categories of "architectural convergence debt." If left unaddressed, every new module added will copy the same flawed patterns, and maintenance cost will scale linearly with the number of files:

1. **Layer boundary leakage** — repositories read global settings, the API hits repositories directly, and services return ORM objects. The three-layer architecture contract (`api/` → `services/` → `repositories/`) exists in name only:
   - `create_from_agent_output` at `repositories/recommendation_repo.py:36` reads `settings.bedrock_model_id` directly, violating the coding-rules.md principle that "Repositories are pure CRUD" — a repository should not know business decisions like "which model to use."
   - Four read endpoints — `api/recommendations.py:11` (`get_recommendation`), `api/recommendations.py:18` (`list_by_customer`), `api/evaluations.py:22` (`get_evaluation`), `api/evaluations.py:30` (`list_by_recommendation`) — inject the repository directly and raise `HTTPException` themselves, bypassing the service layer.
   - `services/pipeline_service.py:37-45` returns the `PipelineJob` ORM, which `_to_response` at `api/pipelines.py:32` then converts in the API layer; `services/evaluation_service.py:35` returns the `Evaluation` ORM and relies on `response_model` for implicit conversion — violating coding-rules.md's "Services do not return SQLModel ORM objects to the API layer."

2. **Consistency debt** — the same "resource not found" error has three coexisting expressions (`repositories/job_repo.py:42` raises `ValueError`, the service layer raises `NotFoundError` from `errors.py`, and the API layer raises `HTTPException` directly), while `main.py:65` has long since registered a global `NotFoundError` handler that only some paths actually flow through. Additionally, `services/agent_service.py:85` uses `getattr(settings, "bedrock_guardrail_id", None)` to read a field that **isn't even declared** in the `config.py` Settings — and because `config.py:12` sets `extra="ignore"`, even if `BEDROCK_GUARDRAIL_ID` is set in the env it gets silently swallowed, so the guardrail never takes effect and there is no warning whatsoever.

3. **Dead code and orphans** — `schemas/canonical.py` (`CanonicalProduct` / `CanonicalCustomer` / `MergedDataset`) is imported by no service, pure dead code; prompt management runs on two parallel tracks: at runtime it actually uses `.md` files (`prompts.py` + the `*_PROMPT_VERSION` constants in `chains/`), but `_select_prompt_variant` at `services/agent_service.py:70` is a `raise NotImplementedError` dead end, `variant_id` is always `None`, and keeping it around only misleads future readers into thinking the DB prompt path is usable.

4. **Documentation and test vacuum** — `docs/architecture/architecture.md` still describes the no-longer-existent `SalesAnalysisService` / `api/analyses.py` / `/analyses/sales/*` endpoints (the files were deleted, leaving only pycache), while the actually-existing `chains/`, `llm.py`, `prompts.py`, `errors.py`, `deps.py`, and `PromoForecastService` (a 451-line orphan service) are completely undocumented — the gap between docs and reality will make any new session's understanding of the architecture flatly wrong. The `tests/` directory does not exist at all, yet `pyproject.toml:67-69` already configures `testpaths=["tests"]` and `asyncio_mode="auto"` and installs pytest / pytest-asyncio — the test infrastructure is ready but there are zero tests, so any refactor (including this one) has no regression safety net.

This change locks in a "standard" scope: **converge only, add no features.** Every change is minimal and independently verifiable, aligned with CLAUDE.md's Simplicity First and forward-only migration principles.

## What Changes

### A. Patch layer boundary leakage

- **A1** — `recommendation_repo.create_from_agent_output` no longer reads `settings.bedrock_model_id`; instead it adds a required parameter `model_id: str` passed in by the caller; the upstream call site `services/pipeline_service.py:84` changes to pass `settings.bedrock_model_id` (it is legitimate for the service layer to read config).
- **A2** — Add a thin read service: introduce `RecommendationService` (two read methods, `get` / `list_by_customer`), and add two read methods `get` / `list_by_recommendation` to `EvaluationService`. The API switches to injecting the service; when a resource is not found, the service raises the domain `NotFoundError` instead of raising `HTTPException` in the API layer. **No interface / abstract base class** — just plain service classes.
- **A3** — Services uniformly return Pydantic DTOs, never ORMs: `PipelineService.create_job` / `get_job` change to return `JobResponse` from `schemas/pipeline.py` (the conversion logic moves from `_to_response` at `api/pipelines.py:32` into the service); `EvaluationService.evaluate` and the new read methods return `EvaluationPublic` from `schemas/public.py`; `RecommendationService` returns `RecommendationPublic`.

### B. Consistency debt

- **B1** — Unify error types: repositories / services uniformly raise `NotFoundError` from `errors.py`, the API layer no longer raises `HTTPException` itself, and everything is handed to the global exception handler already registered at `main.py:65` to convert to 404. The `ValueError` at `repositories/job_repo.py:42` changes to `NotFoundError`.
- **B2** — Guardrail config no longer fails silently: formally declare `bedrock_guardrail_id: str | None = None` and `bedrock_guardrail_version: str | None = None` in the `Settings` of `config.py`; the two `getattr(settings, ...)` calls at `services/agent_service.py:85,89` change to read `settings.bedrock_guardrail_id` / `settings.bedrock_guardrail_version` directly.

### C. Clear dead code and orphans

- **C1** — Delete `src/recommender/schemas/canonical.py`, and accordingly remove the import at `schemas/__init__.py:2-6` and `CanonicalProduct` / `CanonicalCustomer` / `MergedDataset` from `__all__` (`schemas/__init__.py:22-24`).
- **C2** — Converge the dual prompt tracks: establish **`.md` files as the sole runtime prompt source** (loaded by `prompts.py` + the `*_PROMPT_VERSION` constants in `chains/`). Remove `_select_prompt_variant` at `agent_service.py:62-70` (the `NotImplementedError` dead end) and the related dead branch inside `analyze()`; simplify `analyze()` to return `RecommendationOutput` (no longer returning an `(output, variant_id)` tuple), and adjust `pipeline_service.py:77` accordingly. The `PromptVariant` table is **kept, not dropped** (forward-only, retained as future A/B infrastructure), with its "currently not wired up" status noted in the architecture docs.
- **C3** — The 6 HubSpot columns (`hubspot_sync_status` / `hubspot_contact_id` / `hubspot_note_id` / `hubspot_synced_at` / `hubspot_sync_error` / `hubspot_sync_retries` at `models/recommendation.py:51-58`) are **kept untouched**, and only annotated as Phase 4 reserved in the data model section of the architecture docs. See Out of Scope.

### D. Documentation and test vacuum

- **D1** — Sync `docs/architecture/architecture.md` to reflect reality: remove the descriptions of the no-longer-existent `SalesAnalysisService` / `api/analyses.py` / `/analyses/sales/*` (or annotate them as decommissioned, implementation see git history); document the actually-existing `chains/` (LCEL chain factory), `llm.py` (`lru_cache` Bedrock builder), `prompts.py` (`.md` loading), `errors.py` (domain exceptions), `deps.py` (centralized DI), and `PromoForecastService` (451 lines, an orphan service not yet wired to the API).
- **D2** — Create `tests/` and add two categories of tests:
  - **mock_mode e2e**: run the full pipeline with `ANALYZER_MOCK_MODE=true` (POST `/pipelines/run` → query job → query recommendation → POST `/evaluations/{id}` → query evaluation), without hitting Bedrock at any point.
  - **ETL pure-function unit tests**: the deterministic pure functions of `services/promo_forecast_service.py` (`_filter_zhuanhu_dealers` / `_normalize_dealer_id` / `_classify_legal_categories` / `_build_reasoning` / `_rank_opportunities`, etc.), and the aggregation logic of `evaluation_service._build_inputs` (:91). The chain-layer tests inject a LangChain fake chat model (`chains/` is already designed as `build_xxx_chain(llm)` to accept injection).

## Out of Scope (explicitly not done this time)

| Item | Why not |
|------|-----------|
| Wiring `PromoForecastService` to an API router / DI, and externalizing the 33 hardcoded tax IDs (`promo_forecast_service.py:85`) | This is new-feature wiring, not architectural convergence; wiring to the API involves new endpoint design and should be a separate change. This time we only document its existence and orphan status in D1, and add tests for its pure functions in D2 (the tests don't need an API) |
| Dropping the `PromptVariant` table | Forward-only migration principle (safety.md: downgrade / drop loses historical data and is unrecoverable). The table is kept as future prompt A/B infrastructure; the cost is just one idle table |
| Dropping the 6 HubSpot columns | Same forward-only conservative principle as above. Dropping columns requires a migration, is high-risk, and Phase 4 HubSpot sync will definitely use them (`hubspot_sync_status=pending` is already used in the write path, `list_pending_hubspot_sync` already exists). The benefit (saving 5 idle columns) is far smaller than the risk |
| Adding any feature, or actual prompt A/B selection logic | This time is convergence, not expansion. Implement A/B hash-select etc. once the PromptVariant table has data and a real need |
| Adding a DB migration | None of the changes this time touch the schema. `alembic current` must be identical before and after (one of the verification items) |
