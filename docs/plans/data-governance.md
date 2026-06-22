# Phase 1.5: Data Governance / ETL

> ✅ **Status: Done, but with a scope that differs from the original plan. See [§9 Outcome](#9-outcome)**
>
> The next stage after Phase 0 (scaffolding) + Phase 1 (Bedrock integration).
> For the full architectural background see [architecture.md](../architecture.md).

## 1. Goal

Turn `DatasetService.prepare()` from its current stub into a real ETL pipeline:

```
S3 raw (heterogeneous multi-brand CSV)
    ↓
read + parse + apply brand mapper
    ↓
canonical schema validation
    ↓
merge customer + candidate products
    ↓
S3 cleaned (dataset CSV the LLM can consume directly)
```

Produce a `CleaningReport` (rows_in / rows_out / errors / mapping_used) and write it into the corresponding fields of `PipelineJob`.

## 2. Prerequisites Before Entering This Phase

✅ Phase 0 done: three-layer architecture + 4 services + 4 tables + end-to-end mock pipeline running
✅ Phase 1 done: Bedrock Sonnet 4.5 real-LLM integration, runnable with `ANALYZER_MOCK_MODE=false`
⏸ **What must be received before this phase can start**:
   - Real business data samples (at least 1 raw CSV per brand)
   - Customer data sample (customers/customers.csv)
   - Business confirmation of which fields the canonical schema should include

## 3. Questions to Clarify with Business / PO First

### Q1: How much do the raw CSV formats differ across brands?

```
Scenario A: all brands 100% identical format
   → a single default mapper is enough

Scenario B: different brands have different field names / types / encodings
   → one mapper class per brand, brand → mapper registry

Scenario C: schema keeps evolving (new fields / renames)
   → add schema version detection + change alerting
```

### Q2: Which fields should the canonical schema include?

`schemas/canonical.py` already has a `CanonicalProduct` prototype. **Business needs to confirm whether the following fields match requirements**:

```python
class CanonicalProduct:
    sku: str
    product_name: str
    price: float            # need currency?
    stock: int              # need a region dimension?
    category: str           # enum? free text?
    brand: str
    extra: dict             # brand-specific fields
```

Fields that may need to be added (to be confirmed):

- `currency` (multi-currency?)
- `cost` (cost price, for margin analysis?)
- `tags` (category tags?)
- `available_regions` (by region?)
- `release_date` (new / old model?)
- `image_url` / `description` (the LLM might use them?)

### Q3: Merged dataset structure (the version the LLM sees)

The `MergedDataset` in `schemas/canonical.py` assumes "one per customer", with contents:

```python
{
  "customer": CanonicalCustomer (1),
  "candidate_products": [CanonicalProduct]  (N)
}
```

**To confirm**:
- How is the candidate product pool decided? All / those suited to the customer's segment / those newly listed this month?
- Should N=10? 50? 100? (affects prompt token count)
- Does the customer side need historical purchase records attached?

### Q4: Data quality check tiers

```
L1: Pydantic schema validation (required / type)        ← POC must do
L2: business rules (price > 0, stock >= 0, sku format)  ← POC recommended
L3: cross-row checks (sku duplicates / foreign keys / upstream-downstream consistency)  ← do in Phase 2
L4: statistical monitoring (abnormal new-record volume this month)  ← do in Production
```

POC defaults to L2; decide after confirming business needs.

### Q5: Error-handling strategy

When a row fails validation:

```
A. abort the whole ETL (strict)
B. skip the row, record it in CleaningReport.errors, continue
C. fix partially and continue (fill with default values)
```

POC defaults to B (partial fault tolerance); production decides per scenario.

## 4. Work Items (run in order)

### 4.1 Collect data samples (the blocker for this phase)

- [ ] Business or engineering owner provides real raw CSV for at least 1-2 brands (at least 5-10 sample rows)
- [ ] Real customers.csv sample (at least 5 rows, covering different segments)
- [ ] Place them into the corresponding location `products/{category}/{brand}/{year}/{month}/products.csv`
- [ ] Run `scripts/localstack/init-buckets.sh` to re-sync into LocalStack

### 4.2 Design the canonical schema (revise after business review)

- [ ] Confirm / extend `CanonicalProduct` fields in `schemas/canonical.py`
- [ ] Add `validators` for L2 business rules (field_validator + model_validator)
- [ ] Walk through once with business to confirm

### 4.3 Implement the brand mapper abstraction

- [ ] Add a `services/cleaner/mappers/` directory (or `services/dataset/mappers/`)
- [ ] `base.py`: `BrandMapper` Protocol (defines `transform(raw_row) -> CanonicalProduct`)
- [ ] `default_mapper.py`: identity mapping (assumes fields line up)
- [ ] `registry.py`: `brand → mapper` lookup table
- [ ] (if needed) `brand_a_mapper.py`, `brand_b_mapper.py`, etc.

### 4.4 Implement the real logic of `DatasetService.prepare()`

- [ ] Use `S3Service.get_text()` to read the raw CSV
- [ ] Parse with `pandas.read_csv(StringIO(csv_text))`
- [ ] Transform each row with the corresponding brand mapper
- [ ] Validate each row with Pydantic, collect errors into `CleaningReport`
- [ ] Process customer data (read customers.csv, find the row for the matching customer_id)
- [ ] Filter candidate products (rule pending Q3 confirmation)
- [ ] Assemble the `MergedDataset` Pydantic object
- [ ] Write to the S3 cleaned bucket via `MergedDataset.model_dump_json()` or `to_csv()`
- [ ] Return `(cleaned_key, CleaningReport)`

### 4.5 Write the `CleaningReport` into `PipelineJob`

- [ ] Modify `PipelineService.run()` to write `CleaningReport` into `pipeline_job.cleaning_report` (JSONB)
- [ ] Also update the `rows_input` / `rows_output` / `rows_failed` hot columns
- [ ] Add an API endpoint `GET /pipelines/{id}/cleaning-report` returning the full report

### 4.6 Modify `AgentService.analyze` to feed the real dataset

- [ ] Have `_real_analyze()` read the dataset from S3 cleaned
- [ ] Insert the dataset contents into the prompt (mind the token count)
- [ ] Do the formal prompt after the Phase 2 PromptVariant is wired up

### 4.7 Integration testing

- [ ] Run `POST /pipelines/run` with real data → check whether the cleaning report contents are reasonable
- [ ] Run the same customer multiple times → is the recommendation quality stable?
- [ ] Run bad data (deliberately drop fields / change types) → does the ETL fault-tolerance work?

## 5. Out of Scope for This Phase (left for the future)

- ❌ Schema discovery (auto-generate mapping with an LLM) — consider only when multi-brand + frequent format changes
- ❌ Parquet output — CSV is enough at the POC stage; consider only at > 100K rows
- ❌ DuckDB SQL transform — same as above
- ❌ Automatic schema-change detection / migration — Phase 5 production hardening
- ❌ Cross-batch dedup / cross-file consistency — L3 level, do in production
- ❌ ETL visualization dashboard — do only if business actually needs it

## 6. Commands to Start the Next Session

```bash
# 1. Make sure the docker infra is still alive
docker compose -f docker-compose.dev.yml ps

# 2. Confirm lab credentials are not expired (refresh if expired)
./scripts/refresh-lab-creds.sh

# 3. Start FastAPI
set -a && source .env.local && set +a && unset AWS_PROFILE
uv run uvicorn recommender.main:app --reload

# 4. Confirm health is OK
curl http://localhost:8000/health/ready

# 5. Start filling in the prepare() method of src/recommender/services/dataset_service.py
```

## 7. Key References

| Resource | Use |
|------|------|
| [architecture.md](../architecture.md) | Overall architecture and design principles |
| `src/recommender/services/dataset_service.py` | The main file to modify in this phase |
| `src/recommender/schemas/canonical.py` | The schema whose fields need extending |
| `src/recommender/schemas/cleaning.py` | The existing CleaningReport schema |
| `src/recommender/models/job.py` | PipelineJob already has cleaning-statistics columns |
| Pandas docs | https://pandas.pydata.org/docs/ |
| LangChain CSV loader | Reference for later when the prompt needs to feed CSV |

## 8. Suggested First Action for the Next Session

Open `dataset_service.py` and look at the existing stub:

```python
# src/recommender/services/dataset_service.py
async def prepare(self, customer_id, brand, month) -> tuple[str, CleaningReport]:
    """
    TODO Phase 1.5: implement ETL
    1. use self.s3.list_objects() to find the raw CSV
    2. read it with pandas.read_csv
    3. apply the brand mapper to convert to canonical
    4. JOIN customer
    5. filter candidate products
    6. write the cleaned CSV
    7. return cleaned key + CleaningReport
    """
    ...
```

Then take the 5 questions Q1-Q5 to business / PO, and start implementing once you have the answers.

---

## 9. Outcome

> Record date: 2026-05-06
> Status: ✅ Done, but the implementation scope differs from this document's original plan

### 9.1 Why the Scope Changed

**Original plan**: turn `DatasetService.prepare()` from a stub into real ETL, for "per-customer LLM recommendation" (`POST /pipelines/run`). The output format was designed as `MergedDataset` (`{ customer, candidate_products[] }`).

**What we actually found** (after seeing the April xlsx that sales provided):
1. **Customer = dealer** (B2B wholesale), not a consumer — this reframe changes the whole schema interpretation
2. **Data cadence = one file per month** (performance tracking + monthly sales) — there is no such thing as "daily transaction detail"
3. **What we received is a dashboard pivot table** (not a transaction-level extract) — already aggregated, cannot be reversed back to order level
4. **The prompts the business PO raised** are mostly "monthly market analysis across all dealers", not "recommend a product to a specific customer"

→ Conclusion: the per-customer recommendation track has no corresponding raw data at the POC stage, so we **should not force the original plan**. Instead, build a monthly cross-dealer analysis module.

### 9.2 What Was Actually Built

`SalesAnalysisService` (`src/recommender/services/sales_analysis_service.py`) + `/analyses/sales/*` REST API, running fully end-to-end (raw S3 → 3 ETL → Bedrock Sonnet 4.5 → cleaned S3 markdown brief). See [architecture.md §5.5](../architecture/architecture.md) + [§7.2](../architecture/architecture.md).

### 9.3 Mapping the Original Q1-Q5 to Actual Decisions

| Original question | Original framing | Actual decision |
|-------|-----------|--------|
| **Q1** Raw CSV format differences across brands | wanted a brand mapper mechanism | N/A — the sales xlsx is already an aggregated table, no brand-specific raw |
| **Q2** Which fields the canonical schema should include | add fields to `CanonicalProduct` | went with "**hard-code col index in the algorithm**", did not abstract a canonical schema (stable is enough at the POC stage) |
| **Q3** Merged dataset structure | one per customer | changed to "**3 cross-dealer aggregated tables**" (region×category / dealer-tier / cross-sell-gaps) |
| **Q4** Data quality check tiers | L1-L4 | L1 + L2 handled via "hard-coded pivot-table col index" + "`pd.isna()` fault tolerance"; L3-L4 not done |
| **Q5** Error-handling strategy | strict / skip / fix | **skip with fault tolerance** (NaN rows, unrecognized department, unrecognized dealer all silently skipped), POC does not block |

### 9.4 Key Business-Context Points (subsequent sessions must know these)

These facts have been saved to memory (`~/.claude/projects/.../memory/`):

| Memory | Key point |
|--------|------|
| `project_company_customer_means_dealer.md` | The company's "customer" = dealer; customer_id in every schema refers to a dealer ID |
| `project_data_cadence_monthly.md` | Data is one file per month; "today" in prompts really = "this month" |
| `feedback_etl_first_llm_last.md` | The algorithm aggregates first, the LLM only does narrative, never let the LLM do arithmetic |
| `feedback_two_tier_parsing.md` | Default to the algorithm, fall back to the LLM only on format drift (POC does not implement the fallback) |

### 9.5 Business Rules Signed Off by the PO (hard-coded as consts at the top of sales_analysis_service.py)

| Rule | Value |
|------|---|
| **Tier thresholds** | S ≥ 500k / A = 100k-500k / B = 30k-100k / C < 30k |
| **Corresponding actions** | S = sales call + custom EDM / A = standard EDM + LINE / B = mass EDM / C = quarterly re-engagement |
| **Category mapping** (7→6) | tablet → telecom, application peripherals → accessories, others map directly |
| **Region mapping** (5→4) | enterprise-customer sales division → key accounts, others map directly |

### 9.6 Known Technical Debt (left for the future)

| Item | Description | Severity |
|------|------|------|
| ETL hard-codes the month path | `xlsx = Path("aws-s3/sales/2026/04/績效追蹤4月.xlsx")` will blow up in May | 🟡 must change when May arrives |
| CSV has no BOM | Excel opening Chinese CSV shows mojibake (`encoding="utf-8"` without sig) | 🟢 1-line fix |
| Tax-ID column empty | awaiting customer master from IT | 🟢 PO acknowledged |
| LLM hallucinates derived metrics | the LLM computes its own average order value, fabricates timelines like "results in 2 weeks" | 🟡 add a negative constraint before production |
| No `analyses` table | S3 = state, cannot do audit log / version diff | 🟡 add for production |
| No SharePoint sync script | monthly data is currently manually mv'd into aws-s3/ | 🟡 write when May arrives |

### 9.7 What to Do When May's Data Arrives

```
1. Put the new file into aws-s3/sales/2026/05/ (SharePoint sync script TBD, manually mv for now)
2. Write _manifest.json (required for a new month, logical key stays the same)
3. Run init-buckets.sh to sync S3
   docker exec marketing-poc-localstack /etc/localstack/init/ready.d/init-buckets.sh
4. POST /analyses/sales body { month: "2026-05" }
   (the service already accepts the month parameter, no const needs changing)
5. After ~50s, GET /analyses/sales/2026-05/artifacts/market-narrative
```

**Note**: the 3 standalone CLI scripts in `scripts/etl/*.py` hard-code the April path, and **the API does not depend on them**. They are purely quick scripts for dev iteration; you may either delete them or make them also read the month parameter.
