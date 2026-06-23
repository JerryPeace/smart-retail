# Marketing Cleaner POC

Monthly sales data → ETL aggregation → Bedrock LLM market analysis → a decision brief for sales managers.

> Full architecture: [`docs/architecture/architecture.md`](./docs/architecture/architecture.md)

---

## 🚀 Quick Start

```bash
# First time (setup):
cp .env.example .env.local       # environment variables
uv sync                           # install Python deps (recommender + search_engine packages)
open -a OrbStack                  # or start Docker Desktop

# Every time after:
make dev                          # one command: docker + DB migration + FastAPI
```

`make dev` runs, in order:
1. Starts **5 docker containers** (postgres / redis / localstack / adminer / **opensearch**)
2. Waits for postgres + opensearch to be healthy
3. Runs `alembic upgrade head` (DB schema upgrade)
4. Starts FastAPI uvicorn (foreground, Ctrl-C to stop; clears any stale uvicorn first to avoid Errno 48)

After that all 6 services are running. **For product search you still need to build the vector index — see [🔍 Product Search Engine](#-product-search-engine-hybrid-search-setup) below.**

---

## 🔍 Product Search Engine (Hybrid Search) setup

`search_engine` is a standalone module parallel to `recommender`, running **hybrid search over 26k products = BM25 (lexical) + k-NN (Cohere Embed v4 semantic vectors) + min-max fusion**. Details in [`docs/architecture/search-architecture.md`](./docs/architecture/search-architecture.md).

### One-command onboarding (from zero to searchable)

```bash
# 1. After clone, place the 26k product seed at products/OpenSearch_Full_*.json yourself
#    (contains product data, NOT shipped with the public repo; path in scripts/etl/load_products_os.py SOURCE_FILE)
git clone <repo> && cd marketing-recommandation && uv sync

# 2. Refresh AWS lab credentials (vectorization calls Bedrock Cohere v4, needs credentials)
make refresh-creds                # expires ~1hr (refreshes from the live base session, no MFA)

# 3. Start services (brings OpenSearch up too)
make dev                          # foreground; FastAPI starts only after OpenSearch is healthy

# 4. (new terminal) build the vector index: load 26k → full Cohere v4 embedding
make search-setup                 # ⚠️ embed calls real Bedrock, one-off ~$1, ~15–30 min, resumable

# 5. Open the search test UI
open ui/search.html               # plain HTML, hits localhost:8000/search
```

After `make search-setup`, OpenSearch holds the `products_v5_cohere` index (26,014 docs fully embedded). Open `ui/search.html` and try queries like "cold hands and feet in winter", "air fryer", "neck and shoulder pain from sitting" to see results (only relevance ≥ 0.26 shown).

### Key points & common pitfalls

| Item | Notes |
|------|------|
| **Vector model** | Cohere Embed v4 (`cohere.embed-v4:0`) / 1536 dims / region `ap-northeast-1`. Index `products_v5_cohere`, fusion weight `w_bm25=0.2` (configurable). |
| **Credentials** | App vectorization uses the `lab` profile with **auto-renewal** (`aws_profile=lab`); static `.env.local` credentials are capped at ~1h by AWS role chaining, so rerun `make refresh-creds` to refresh them from the live base session. |
| **search 500** | Usually expired credentials. Run `make refresh-creds` then **restart `make dev`** (credentials are read at process start). |
| **Cost** | `make search-embed` (part of search-setup) is 26k × Cohere v4 ≈ <$1 one-off. Reruns are idempotent — only fills gaps, no double charging. |
| **Rebuild / switch index** | `make search-setup SEARCH_INDEX=products_v6` targets a different index name. |

---

## 📦 Services (Docker stack)

| Service | Port | Purpose | How to inspect |
|------|------|------|--------|
| **FastAPI** | 8000 | API server | http://localhost:8000/docs (Swagger UI) |
| **Postgres** | 5434 | Main DB (PipelineJob / Recommendation tables) | http://localhost:8081 (Adminer) |
| **Redis** | 6380 | Reserved for POC (unused for now) | `redis-cli -p 6380 -a redispoc PING` |
| **LocalStack** | 4567 | Mock AWS S3 (raw / cleaned buckets) | `awslocal --endpoint-url=http://localhost:4567 s3 ls` |
| **Adminer** | 8081 | Postgres GUI | http://localhost:8081 (server: postgres / user: poc / pass: poc / db: marketing_cleaner) |
| **OpenSearch** | 9200 | Product hybrid search (k-NN + BM25) | `curl localhost:9200/_cat/indices` (look for `products_v5_cohere`) |

**Why these ports?** Offset from intellio.ai's existing docker stack so both can run side by side without conflict (intellio.ai uses 5433 / 6379 / 4566).

---

## 🛠 Make Commands (unified interface)

```bash
make help                         # list all commands
```

### Start / stop

```bash
make dev                          # start everything (infra + migration + FastAPI)
make infra-up                     # only the 5 docker containers (incl. opensearch), no FastAPI
make infra-status                 # docker service status
make infra-down                   # stop docker (keep DB data)
make infra-clean                  # ⚠️ stop docker + wipe volumes (DB data is lost!)
```

### Develop / operate

```bash
make migrate                      # run alembic upgrade only (infra must be up)
make api                          # start FastAPI only (no docker restart)
make health                       # health check (FastAPI + docker containers)
```

### Product search (vector index)

```bash
make search-setup                 # 🔍 build index in one go: load 26k + full Cohere v4 embedding (⚠️ ~$1 Bedrock)
make search-load                  # build index + load products only (no embedding, free)
make search-embed                 # ⚠️ Cohere v4 vectorization only (real Bedrock, resumable)
make search-verify Q=cold-hands   # search smoke test (curl /search)
```

### AWS

```bash
make refresh-creds                # refresh lab temporary credentials (run when ~1hr expires, no MFA)
```

### Run analysis (end-to-end demo)

```bash
make analyze MONTH=2026-04        # trigger a month's analysis (runs in background ~50s)
make list-analyses                # list analyzed months
make narrative MONTH=2026-04      # pull the markdown brief
```

### ETL standalone (without the API)

```bash
make etl-april                    # run April's 3 ETL scripts, output to out/
```

---

## 🐳 Docker startup details

### First: make sure the Docker daemon is running

On macOS we recommend **OrbStack** (lightweight, fast, a Docker Desktop replacement):

```bash
brew install --cask orbstack       # first-time install
open -a OrbStack                   # start
```

Confirm the daemon is reachable:
```bash
docker ps                          # no error = OK
```

### Start the 5 infra containers

```bash
make infra-up
# equivalent to:
# docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres redis localstack adminer opensearch
```

The first start does 3 things:
1. **Pulls images** (postgres:17 / redis:7-alpine / localstack:3.8 / adminer:latest / opensearch:2.19.x)
2. **Creates named volumes** (`postgres_data`, `redis_data`, `localstack_data`, `opensearch_data`) — data persistence
3. **LocalStack `ready.d` auto-runs `scripts/localstack/init-buckets.sh`** — creates raw-data / cleaned-data buckets + syncs `aws-s3/` up

### Confirm all containers are healthy

```bash
make infra-status
```

Expected:
```
NAME                          STATUS
marketing-poc-postgres        Up X seconds (healthy)
marketing-poc-redis           Up X seconds (healthy)
marketing-poc-localstack      Up X seconds (healthy)
marketing-poc-adminer         Up X seconds
marketing-poc-opensearch      Up X seconds (healthy)
```

If STATUS is `(starting)`, wait 30s and re-check. If `(unhealthy)` or `Restarting`, run `make infra-down` then `make infra-up`.

### Start FastAPI (after infra is ready)

```bash
make api
```

Or all at once: `make dev` (includes migration + FastAPI).

### Stop

```bash
make infra-down                    # stop containers, keep volumes (data survives next up)
make infra-clean                   # ⚠️ also wipe volumes, DB reset
```

### Common errors

| Message | Cause | Fix |
|------|------|------|
| `Cannot connect to the Docker daemon at unix:///...orbstack` | OrbStack not started | `open -a OrbStack` |
| `port is already allocated` | 5434/6380/4567/8081 taken | `lsof -i :{port}` to find the owner, or stop it |
| `database "marketing_cleaner" does not exist` | first start, init script not done yet | wait 30s, or `make infra-down` then up |
| FastAPI 401 / search 500 on Bedrock | lab credentials expired (~1 hour) | `make refresh-creds` then **restart `make dev`** (credentials read at process start) |
| `make dev` reports `Errno 48 Address already in use` | stale uvicorn holding 8000 | dev.sh already self-cleans; if still stuck `lsof -ti:8000 \| xargs kill -9` |

---

## 🔥 Run a full end-to-end

```bash
# 1. Start services
make dev    # in another terminal, since dev is foreground

# 2. (new terminal) trigger April analysis
make analyze MONTH=2026-04
# wait ~50s (99% is Bedrock latency)

# 3. View the analysis report
make narrative MONTH=2026-04 | head -50

# 4. Or click around in Swagger UI
open http://localhost:8000/docs
```

### April data (place it yourself)

> Raw sales data contains business confidential information and is **NOT shipped with the public repo** (`aws-s3/` is gitignored). Put the April sales xlsx + manifest under `aws-s3/sales/2026/04/`; LocalStack auto-syncs it into S3 on startup, and you can then run `make analyze MONTH=2026-04`.

The handling flow for future months (e.g. May) is in [`docs/plans/data-governance.md` §9.7](./docs/plans/data-governance.md).

---

## 🗂 Project structure

```
.
├── Makefile                                ⭐ unified operation interface
├── README.md                               this file
├── pyproject.toml / uv.lock / .python-version
├── Dockerfile                              multi-stage uv build
├── docker-compose.dev.yml                  local 5-container definition (incl. opensearch)
├── .env.local                              environment variables (copied from .env.example)
│
├── alembic/ + alembic.ini                  DB migration
│
├── scripts/
│   ├── dev.sh                              ← `make dev` (start infra incl. opensearch + self-clean + FastAPI)
│   ├── refresh-lab-creds.sh                ← `make refresh-creds` (lab credentials ~1h, no MFA)
│   ├── localstack/init-buckets.sh          LocalStack ready.d auto-creates buckets + syncs fixtures
│   ├── db/                                 DB reset / dump tools
│   └── etl/                                ETL + search CLIs (load_products_os / embed_products_os / judge…)
│
├── src/                                    ⭐ two top-level packages
│   ├── recommender/                        🟦 marketing recommendation pipeline (api/services/repositories 3 layers + chains)
│   └── search_engine/                      🟪 product hybrid search (standalone module, mounted on same app, shares recommender.config)
│       └─ router/service/repository/fusion/embeddings/client/schemas; layer roles in architecture.md §5.8
│
├── ui/search.html                          plain HTML search test UI (hits localhost:8000/search)
├── products/OpenSearch_Full_*.json   ⭐ 26k product seed (gitignored, NOT in public repo; place it yourself, used by make search-setup)
│
├── aws-s3/                                 ⭐ S3 source of truth (local mirror, gitignored, NOT in public repo)
│   ├── products/{category}/{YYYY}/{MM}/products.csv
│   ├── customers/customers.csv
│   └── sales/{YYYY}/{MM}/             monthly sales files (originals not renamed + manifest)
│       └── 04/{xlsx files} + _manifest.json
│
├── out/                                    ETL local output (gitignored)
└── docs/
    ├── architecture/architecture.md         ⭐ main architecture doc (read this!)
    └── plans/
        ├── README.md
        └── data-governance.md               Phase 1.5 plan + actual outcome
```

---

## 🔑 Key environment variables (`.env.local`)

| Variable | Default | Notes |
|------|------|------|
| `ANALYZER_MOCK_MODE` | `true` | true = return fixtures / **search vectorization also mocked (returns fixed vectors, meaningless results)**; set `false` for real search hitting real Cohere |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Sonnet 4.5 cross-region inference profile (the `us.` prefix is required) |
| `BEDROCK_REGION` | `us-east-1` | |
| `AWS_ACCESS_KEY_ID` / `_SECRET_` / `_SESSION_TOKEN` | (written by refresh-creds.sh) | lab role temporary credentials (~1 hour expiry, AWS role chaining cap) |
| `AWS_PROFILE` | `lab` | boto3 uses this profile for search vectorization with **auto-renewal** (config default `lab`, no need to set) |
| **`BEDROCK_EMBED_MODEL_ID`** | `cohere.embed-v4:0` | product search vector model (Cohere Embed v4) |
| **`BEDROCK_EMBED_REGION`** / **`EMBED_DIMENSIONS`** | `ap-northeast-1` / `1536` | embedding region and dimensions |
| **`OPENSEARCH_INDEX`** / **`SEARCH_BM25_WEIGHT`** | `products_v5_cohere` / `0.2` | search index name and fusion weight |
| `AWS_ENDPOINT_URL_S3` | `http://localhost:4567` | set = use LocalStack, empty = real AWS |
| `S3_RAW_BUCKET` / `S3_CLEANED_BUCKET` | `raw-data` / `cleaned-data` | |
| `S3_ROOT_PREFIX` | `marketing-recommandation` | all keys live under this prefix |
| `DATABASE_URL` | `postgresql+asyncpg://poc:poc@localhost:5434/marketing_cleaner` | |

---

## 🧬 Prompt Version Management

Prompts always go through **versioning, never hardcoded**. There are two complementary paths:

### 1. In-repo file versioning (default, for technical prompts that travel with the code)

The prompt body lives in `prompts/{module}/{version}.md`, compiled into a LangChain `ChatPromptTemplate` by `recommender.prompts.load_system_prompt()`. The version = file name + the caller's `*_PROMPT_VERSION` constant; **published means immutable** — to change content, publish a new version (`v1.1`) and update the constant rather than editing in place (avoids mixing old/new during A/B).

| prompt | file | version constant | purpose |
|--------|------|----------|------|
| recommendation | `prompts/recommendation/v1.0.md` | `chains/recommendation.py: RECOMMENDATION_PROMPT_VERSION` | recommendation report generation |
| judge | `prompts/judge/v1.0.md` | `chains/judge.py: JUDGE_PROMPT_VERSION` | LLM-as-judge evaluation |

### 2. LangSmith Prompt Hub (when you want UI editing, A/B, or decoupling from deploys)

LangSmith tracing is already enabled via environment variables (see `agent_service._build_llm`). Prompt Hub adds centralized version control:

```python
from langsmith import Client
client = Client()                      # reads LANGSMITH_API_KEY (from .env.local)

# one push = one commit (unique hash); private by default
client.push_prompt("my-prompt", object=chat_prompt_template)

# pull: latest / by tag (prod, dev) / pin by commit hash
prompt = client.pull_prompt("my-prompt")
prompt = client.pull_prompt("my-prompt:production")
prompt = client.pull_prompt("my-prompt:<commit_hash>")
```

| Aspect | How |
|------|------|
| **Versioning** | each `push_prompt` creates a commit; the UI **Commits** tab shows diffs, rollback, pinning |
| **Switch without code change** | tag a stable version (`production` / `dev`), pull with `pull_prompt("name:production")`, switch versions by moving the tag |
| **A/B** | `client.evaluate(target, data=..., experiment_prefix="v1")` runs each version once; or `evaluate((expA, expB), evaluators=[...])` for pairwise comparison (needs `langsmith>=0.2.0`) |
| **Access** | prompts are **private** by default (only your workspace sees them); going public requires opt-in and a shared workspace handle |

> **Confidentiality principle**: prompts containing sensitive business content are **not committed to this repo** — they live in LangSmith **private** only; only technical prompts that travel with the code (recommendation / judge) go under `prompts/`. Rule of thumb: tech can be public, go-to-market plays stay hidden. `LANGSMITH_API_KEY` lives in `.env.local` (gitignored) and is **never committed**.

For structuring long prompts / large inputs (data first, query last, `<documents>` wrapping, ground-in-quotes), see [Claude's official long-context prompting guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices).

---

## 📚 Further reading

- [`docs/architecture/architecture.md`](./docs/architecture/architecture.md) — full architecture (3 layers + DI + two pipelines + S3 layout + Bedrock integration)
- [`docs/architecture/search-architecture.md`](./docs/architecture/search-architecture.md) — 🔍 **authoritative search subsystem doc** (Cohere v4 + BM25 hybrid, min-max fusion, failure modes, diagrams)
- [`docs/plans/data-governance.md`](./docs/plans/data-governance.md) — Phase 1.5 ETL plan + actual outcome (§9 covers tech debt + May implementation steps)

---

## 🎯 Phase status at a glance

| Phase | Scope | Status |
|-------|------|------|
| 0 | Scaffolding | ✅ |
| 1 | Real Bedrock integration | ✅ |
| 1.5 | Real ETL logic | ✅ scope pivot, implemented sales analysis |
| 1.6 | `/analyses/sales` API + Bedrock narrative | ✅ |
| 2 (search) | 🔍 product hybrid search (`search_engine` module: Cohere v4 + BM25 + min-max, `GET /search`) | ✅ |
| 2 | Prompt management | ⏸ |
| 3 | Evaluation pipeline (LLM-as-judge) | ⏸ |
| 4 | SharePoint → S3 auto-sync script | ⏸ |
| 5 | HubSpot Renderer + Sync | ⏸ |
| 6 | Production hardening | ⏸ |
