# Spec: architecture-convergence

This spec defines the four "contracts" after convergence: the layering contract, the error-handling contract, the settings contract, and the testing contract. Once implementation is complete, any code that violates one of the Requirements below is considered to have failed acceptance.

## ADDED Requirements

### Requirement: Layering contract — the API layer depends only on services

API routers (`src/recommender/api/*.py`) SHALL only inject services (via `deps.py`'s `*ServiceDep`), SHALL NOT inject repositories, SHALL NOT perform ORM→DTO conversion, and SHALL NOT raise `HTTPException`.

#### Scenario: read endpoints go through a service
- **WHEN** a client calls `GET /recommendations/{id}`, `GET /recommendations/by-customer/{id}`, `GET /evaluations/{id}`, `GET /evaluations/by-recommendation/{id}`, or `GET /pipelines/{id}`
- **THEN** the router only calls the corresponding service method and returns its result, with no `if x is None: raise` branch in the function body

#### Scenario: static check of the API layer
- **WHEN** running `grep -rn "HTTPException\|RepoDep" src/recommender/api/`
- **THEN** `HTTPException` has 0 hits; `*RepoDep` injections have 0 hits

### Requirement: Layering contract — the Service layer returns Pydantic DTOs

The public methods a service exposes to the API layer SHALL return Pydantic DTOs (`RecommendationPublic` / `EvaluationPublic` / `JobResponse`), and SHALL NOT return SQLModel ORM objects. The service is the only layer permitted to read `settings`, perform cross-repository orchestration, and do ORM→DTO conversion.

#### Scenario: return types of each service
- **WHEN** inspecting the return type annotations of `RecommendationService.get/list_by_customer`, `EvaluationService.evaluate/get/list_by_recommendation`, and `PipelineService.create_job/get_job`
- **THEN** they are respectively `RecommendationPublic` (including list), `EvaluationPublic` (including list), and `JobResponse`, with no `-> Recommendation` / `-> Evaluation` / `-> PipelineJob`

#### Scenario: thin read services are not over-designed
- **WHEN** inspecting the read methods of `RecommendationService` and `EvaluationService`
- **THEN** they are plain async methods on a plain class (with the repo injected via the constructor); no new abstraction such as an interface, abstract base class, or generic base exists

### Requirement: Layering contract — the Repository layer is pure CRUD

Repositories (`src/recommender/repositories/*.py`) SHALL only do CRUD: not import `recommender.config`, contain no business decisions, and not call external services. Business parameters (such as `model_id`) SHALL be passed in by the caller as arguments.

#### Scenario: create_from_agent_output does not read settings
- **WHEN** `PipelineService.run()` writes a recommendation
- **THEN** the `model_id` of `create_from_agent_output(..., model_id=...)` is passed in by the service (the value being `settings.bedrock_model_id`, identical to what was written before convergence)
- **AND** `grep -rn "from recommender.config import settings" src/recommender/repositories/` has 0 hits

### Requirement: Error-handling contract — a single NotFoundError flow

"Resource not found" SHALL have exactly one expression: `recommender.errors.NotFoundError`. A repository's read methods (the `get` family) return `None`, and the service decides and then raises `NotFoundError`; when a precondition lookup on a repository write path fails (such as `job_repo.update_status`), it raises `NotFoundError` directly. The API layer neither raises nor catches it — the exception is converted to HTTP 404 by the global handler at `main.py:65` (`{"detail": str(exc)}`), and unexpected exceptions are converted to 500 by the handler at `main.py:71`.

#### Scenario: missing resource returns 404
- **WHEN** a client calls `GET /pipelines/{id}`, `GET /recommendations/{id}`, `GET /evaluations/{id}`, or `POST /evaluations/{id}` with a nonexistent id
- **THEN** it returns `404` + JSON body `{"detail": "... not found"}`

#### Scenario: repositories no longer raise ValueError to express not-found
- **WHEN** `JobRepository.update_status` receives a nonexistent `job_id`
- **THEN** it raises `NotFoundError`, and `grep -rn "raise ValueError" src/recommender/repositories/` has 0 hits

#### Scenario: BackgroundTask exceptions do not go through the HTTP handler
- **WHEN** `PipelineService.run()` (inside a BackgroundTask) encounters an exception
- **THEN** the existing behavior is preserved: `logger.exception` + mark the job `failed` + re-raise, without expecting the HTTP handler to intervene

### Requirement: Settings contract — guardrail fields are formally declared

`config.py`'s `Settings` SHALL declare `bedrock_guardrail_id: str | None = None` and `bedrock_guardrail_version: str | None = None`. Code SHALL NOT read any Settings field via `getattr(settings, ...)` (that is a cover for "the field might not exist," which silently lets settings fail).

#### Scenario: env var binds correctly
- **WHEN** the environment sets `BEDROCK_GUARDRAIL_ID=gr-xxx` and the app starts
- **THEN** `settings.bedrock_guardrail_id == "gr-xxx"`, and `AgentService._guardrail_config()` returns a dict containing `guardrailIdentifier` (falling back to `"DRAFT"` when version is unset)

#### Scenario: behavior unchanged when unset
- **WHEN** the guardrail env var is not set
- **THEN** `_guardrail_config()` returns `None`, and LLM calls carry no guardrailConfig (identical to before convergence)

#### Scenario: no getattr(settings across the whole codebase
- **WHEN** running `grep -rn "getattr(settings" src/recommender/`
- **THEN** 0 hits

### Requirement: Prompt contract — .md files are the sole runtime source

Runtime prompts SHALL come only from `prompts/{module}/{version}.md`, loaded by `prompts.py:load_system_prompt`, with the version specified by the `*_PROMPT_VERSION` constants in `chains/`. The `PromptVariant` table and `prompt_variant_repo` SHALL be retained (dormant, forward-only), but there SHALL NOT exist any runtime dead end pointing to them (a `NotImplementedError` stub, or an always-`None` variant-parameter chain).

#### Scenario: dead ends removed
- **WHEN** running `grep -rn "NotImplementedError" src/recommender/services/` and `grep -rn "prompt_variant_id" src/recommender/services/`
- **THEN** both have 0 hits; `AgentService.analyze()` returns `RecommendationOutput` (not a tuple)

#### Scenario: table retained and documented
- **WHEN** inspecting alembic and the architecture document
- **THEN** there is no migration to drop `prompt_variant`; `docs/architecture/architecture.md` annotates the table as dormant (not wired up, kept as A/B infrastructure)

### Requirement: Data-model contract — forward-only, zero migration

This change SHALL NOT add, modify, or delete any DB schema. The 6 HubSpot columns (`models/recommendation.py:51-58`) SHALL be kept untouched and annotated as Phase 4 reserved in the architecture document.

#### Scenario: alembic state unchanged
- **WHEN** comparing the `alembic current` output and the `alembic/versions/` directory before and after implementation
- **THEN** the revision is identical and there are no new files

### Requirement: Documentation contract — architecture.md reflects the real structure

`docs/architecture/architecture.md` SHALL be consistent with the actual file structure of `src/recommender/`: not describing modules that no longer exist (`SalesAnalysisService` / `api/analyses.py` / `/analyses/sales/*`), and documenting all top-level modules that actually exist (`chains/`, `llm.py`, `prompts.py`, `errors.py`, `deps.py`, and `PromoForecastService` including its "orphaned, not wired to the API" state).

#### Scenario: document aligns with the file system
- **WHEN** comparing the document's directory tree with the output of `find src/recommender -name "*.py"`
- **THEN** the document contains no deleted files; the six existing modules above each have a corresponding section or directory-tree entry

### Requirement: Testing contract — coverage levels and mock strategy

`tests/` SHALL exist and `pytest` SHALL be all green, covering three levels, with zero Bedrock calls (zero cost) throughout:

1. **e2e (HTTP→service→repo→DB)**: run the full pipeline and evaluation flows + the 404 failure path under mock mode (`ANALYZER_MOCK_MODE=true`). The DB uses the docker-compose dev Postgres.
2. **ETL units (pure functions)**: the deterministic functions of `promo_forecast_service` and `evaluation_service._build_inputs`, with no DB and no network — guarding the algorithmic aggregation layer of "ETL First, LLM Last".
3. **chain assembly (fake LLM injection)**: `build_recommendation_chain` / `build_judge_chain` injected with a fake chat model, verifying the prompt-variable and output-type contract.

#### Scenario: mock e2e does not call Bedrock
- **WHEN** running the e2e tests under `ANALYZER_MOCK_MODE=true`
- **THEN** the pipeline reaches `done`, the evaluation's `judge_model_id == "mock"`, and there are no AWS Bedrock network calls

#### Scenario: unit tests are independent of infrastructure
- **WHEN** running `pytest tests/test_etl_units.py tests/test_chains.py` standalone in an environment with no docker and no AWS credentials
- **THEN** all pass

#### Scenario: tests guard the error-handling contract
- **WHEN** the e2e failure-path tests hit the read endpoints with a nonexistent id
- **THEN** they receive 404 + `{"detail": ...}`, proving the NotFoundError → global handler chain is effective
