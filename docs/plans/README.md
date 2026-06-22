# Plans

Work-plan documents for each phase. Each plan covers: goals, prerequisites, open questions, work items, and what is out of scope.

| Plan | Phase | Status |
|------|-------|--------|
| [data-governance.md](./data-governance.md) | 1.5 — ETL data governance | ✅ Done (scope pivot; actually built `/analyses/sales` monthly analysis, see §9 of the doc) |
| [promo-forecast-data-fitness.md](./promo-forecast-data-fitness.md) | 2.0 — Key-account promo forecast POC | 🟡 Awaiting PO alignment on 4 key questions before kickoff (data-fitness conclusion already done) |
| [promo-forecast-moea-business-scope.md](./promo-forecast-moea-business-scope.md) | 2.0 Action 3 — MOEA registered-business inventory for 33 key accounts | ✅ Batch query done; awaiting PO sign-off to expand the mapping (~24 entries) |
| [product-search-vectorization.md](./product-search-vectorization.md) | 3.0 — E-commerce product semantic/Hybrid search POC (local docker OpenSearch + Titan v2) | 🟡 Plan pending review; Phase 1 = load raw data into local OpenSearch + Titan v2 vectorization |

> For the full architectural background see [../architecture/architecture.md](../architecture/architecture.md)
> Key business-context facts live in memory: `~/.claude/projects/.../memory/MEMORY.md`
