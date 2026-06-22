# Search Subsystem Architecture (Hybrid Product Search)

> This document is the **complete, up-to-date** architecture description of the `search_engine` module (`src/search_engine/`) and the authoritative reference for the search subsystem; §5.8 of the global [`architecture.md`](./architecture.md) is a high-level summary that points back here. Production uses **min-max score fusion** + **Cohere Embed v4 / 1536 dimensions** (vectorization, index `products_v5_cohere`). For the deep tuning process, failure modes, and the rationale behind decisions, see [`../plans/search-tuning-decision-record-20260613.md`](../plans/search-tuning-decision-record-20260613.md).

## 1. Positioning and Scope

A local POC for **semantic / hybrid search** over the company's 26k products. Core thesis: pure lexical search (BM25) cannot find symptom↔benefit queries like "cold hands and feet in winter → hand warmer", so semantic vectors are needed to fill the gap; but pure vectors suffer from polarity blindness (cold↔hot) and insufficient lexical precision (model number / brand), so we adopt **hybrid = BM25 + k-NN fusion**.

- **Not in prod**: local docker OpenSearch + Bedrock API to validate search quality.
- **Standalone module**: `search_engine` is a top-level module parallel to `recommender` (its storage infrastructure is OpenSearch + Cohere embedding, different from the core PostgreSQL+Bedrock); it mounts on the same app and reuses recommender.config (rationale in architecture.md §5.8).

## 2. High-level Architecture: Two Legs + Fusion

```
query → set bm25_weight → two legs concurrently → min-max fusion → DTO
                          ├─ vector leg (k-NN, Cohere v4 dense)
                          └─ lexical leg (BM25, smartcn tokenization)
```

- **Lexical leg (BM25)**: the query goes through smartcn Chinese tokenization → `multi_match` against `martName/feature/keyword`. Strong on model numbers / brands / exact product names; weak on symptom descriptions, and with Traditional Chinese, smartcn degrades into per-character splitting so a single character like "腳" (foot) causes collisions.
- **Vector leg (k-NN)**: the query is **embedded into 1536 dimensions by Cohere Embed v4** (`input_type=search_query`, L2-normalized) → faiss/hnsw/innerproduct k-NN. Strong on semantics / symptoms; its Chinese semantic depth beats the previous-generation Titan v2 (real retrieval tests show it substantially mitigates the "cold hands and feet → cooling" polarity blindness), but a residual tail of polarity and long-tail issues remains.
- **Fusion**: **min-max score fusion** on the application side—each leg's raw `_score` is per-query min-max normalized, then weighted and summed: `fused = w_knn·norm(knn) + w_bm25·norm(bm25)`, where `w_knn = 1 - w_bm25`. **Current prod is `w_bm25=0.2`** (re-tuned down from the Titan-era 0.7 after switching to Cohere v4—the vector leg got cleaner, so the optimal weight shifted toward the vector side).

> ⚠️ **The `w_bm25=0.7` that appears in the "why min-max" section below is historical analysis from the Titan era** (explaining the RRF→min-max motivation), not the current value. Switching the model = switching the optimal fusion weight: Cohere v4's w-sweep on 4 flagship queries shows symptom queries are only correct at `w_bm25≤0.2`, while keyword queries are stable at any w, so prod settles on 0.2 (see the `config.search_bm25_weight` comment).

### Why min-max instead of RRF

This project **used RRF in its first Phase 2 version** (equal-weight `Σ 1/(k+rank)`), and only later switched to weighted min-max (commit `828507f`: "swap equal-weight RRF for weighted min-max, hybrid hits 79"). The reason for the switch is two structural limits of RRF:

| Aspect | RRF | min-max | Impact on this project |
|------|-----|---------|----------------|
| Fusion basis | only looks at **rank** (which position) | preserves **raw _score** magnitude | for exact model-number queries (ThinkPad), BM25 crushes—"45 points blowing away 8 points"—for the top result; RRF treats this as a single positional gap of "rank 1 vs rank 2" and cannot express strong confidence |
| Weighting | original is **equal-weight** (both legs treated alike) | built-in tunable `w_bm25` | equal-weight RRF dilutes the stronger leg with the weaker one, so the hybrid fusion score cannot beat BM25 alone; only after setting `w_bm25=0.7` to let BM25 dominate does it score `79 > bm25 76 > knn 65` on the 15-item golden set |
| prod/offline consistency | — | aligns with `minmax_fusion` in `investigate_hybrid_fusion.py` | online results = the 79 measured during offline tuning (see the `fusion.py` docstring) |

**Honest caveat (so we don't oversell min-max as a silver bullet)**: ① the real leverage is "**weighting**", not min-max itself—weighted RRF (`Σ wᵢ/(k+rank)`) can weight just as well; choosing min-max was half about aligning with the offline tuning harness. ② Re-validation on a 50-item golden set + bootstrap shows the hybrid−bm25 margin **falls within the noise band (CI crosses 0)**, and `w_bm25=0.7` overfits (see [`../plans/search-tuning-decision-record-20260613.md`](../plans/search-tuning-decision-record-20260613.md))—so the correct conclusion is "min-max **lets us weight**, and weighting cleared the 15-item threshold, but the advantage is fragile". After switching to Cohere v4, **the best fusion weight for all query types (symptom / keyword / brand / model number) converges to the same low value `w_bm25=0.2`**, leaving no "different types need different weights" headroom to squeeze; the real improvement comes from **switching the model + fixing a low weight**, not from the fusion formula or dynamic weight tuning. ③ The cost of min-max: it is sensitive to outlier high scores (one extremely high score squashes the other docs toward 0) and needs per-query normalization boundary handling (`fusion.py:_normalize`'s `hi==lo→1.0`). For exactly this reason, `reciprocal_rank_fusion` is kept as a tuning-free baseline and not deleted.

## 3. Module Structure (`src/search_engine/`)

| File | Responsibility |
|------|------|
| `router.py` | `GET /search`: validates `q` (required) / `size` (1–100) / `bm25_weight` (optional 0–1 manual override); injects `SearchServiceDep`, does not touch the client, does not raise HTTPException |
| `service.py` | `SearchService.search(query, size, bm25_weight)` orchestrates the full chain: weight resolution → embed → msearch → fusion → DTO. The mock decision happens here |
| `repository.py` | Pure I/O: `build_knn_body`/`build_bm25_body` build the DSL, `hybrid_msearch` runs both legs concurrently in a single msearch |
| `fusion.py` | Pure-function fusion: `min_max_score_fusion` (used in prod) + `reciprocal_rank_fusion` (kept, used only in unit tests). Zero I/O, testable |
| `embeddings.py` | `@lru_cache get_bedrock_embeddings(...)` returns a cached Cohere v4 query embedder (boto3, `input_type=search_query`, `output_dimension=1536`, L2-normalized, non-blocking via `asyncio.to_thread`); `MOCK_QUERY_VECTOR` (a fixed 1536-dim unit vector) |
| `client.py` | `@lru_cache get_opensearch_client()` returns an `AsyncOpenSearch` singleton; `close_opensearch_client()` for lifespan shutdown |
| `schemas.py` | `SearchResultItem` / `SearchResponse` DTOs; includes the observability fields `applied_bm25_weight` / `route_label` (currently only ever "manual" or None) |

> Within the module we still follow the router→service→repository three-layer responsibilities; DI wiring lives only in `deps.py`; the async OpenSearch client lifecycle hooks into the `main.py` lifespan.

## 4. Data Plane (Indexes)

| Index | analyzer (BM25 leg) | embedding (vector leg) | Status |
|------|------|------|------|
| **products_v5_cohere** | smartcn (Traditional Chinese per-character split) | **Cohere Embed v4, 1536 dims** | ✅ **prod default** (Chinese semantic retrieval far better than Titan, decisively wins in real retrieval tests) |
| products_v1 | smartcn | Titan v2, 1024, includes feature | former prod (Titan baseline, golden-set hybrid 228) |
| products_v2 | smartcn + Traditional→Simplified (stconvert) | copy of v1 | experiment (changed tokenization → 217 overall, worse) |
| products_v3 | t2s words + cjk_bigram multi-field | copy of v1 | experiment (212, worst) |
| products_v4_nofeat | smartcn | Titan v2, feature removed | experiment (225 ≈ v1 but fixes vector pollution) |

**Embedding invariant**: query and doc must use the same model, same dimension, same normalization (**Cohere Embed v4 / `output_dimension=1536` / L2-normalized + innerproduct**). Cohere float embeddings are not unit-length, so both the doc side (`embed_products_os.py`) and the query side (`embeddings.py`) **manually L2-normalize at both ends**, so that innerproduct is equivalent to cosine; if either side skips normalization, the two vectors live in different spaces and the k-NN scores are silently all wrong. Also: doc side uses `input_type=search_document`, query side uses `input_type=search_query` (Cohere's asymmetric encoding).

## 5. Known Failure Modes and Design Decisions (see the decision record for details)

> Note: most of the "failure modes" in the table below were observed in the **Titan v2 era**. After switching to **Cohere Embed v4** (i.e. prod `products_v5_cohere`—note this "v4" is the Cohere model version, different from the Titan feature-removed experimental index `products_v4_nofeat` mentioned below), A/B real-retrieval testing shows they are substantially mitigated.

| Failure mode | Example (Titan era) | Root cause | Current decision (after Cohere v4) |
|---|---|---|---|
| **A Polarity blindness** | cold hands and feet → cooling fan | dense embedding clusters by topic and ignores direction (NevIR: all dense ≈ random) | **Cohere v4's Chinese semantic depth is sufficient; real-retrieval tests show substantial mitigation** (cold hands and feet → warm gloves, not cooling); the residual tail is accepted. Note: polarity is a common ailment of the dense family, but "insufficient Chinese quality" amplifies it, and switching to a better model takes effect immediately |
| **B feature pollution** | prolonged-sitting neck/shoulder soreness → Bluetooth earbuds | feature marketing boilerplate (comfortable for long wear / ergonomic) pollutes the vector | **Cohere v4 is less affected by boilerplate pollution** (measured: neck/shoulder soreness → massage shawl / neck-shoulder heat pad, not earbuds); so the v5 embedding text still includes feature. The Titan-era feature-removal experiment (`products_v4_nofeat`) is no longer needed |
| **C CJK tokenization** | cold hands and feet → tripod | smartcn splits Traditional Chinese per character, so "腳" (foot/leg) collides (**a BM25-leg problem, unrelated to the vector model**) | Traditional→Simplified / bigram were both tried and were worse → the BM25 leg keeps smartcn; once the Cohere vector leg is clean, fusion is more stable, and fixing a low bm25 weight works around it |

**Not adopted**: query-type routing has been **removed** (after switching to Cohere v4, the best weight for all query types converges to the same low value `w_bm25=0.2`, the "different types need different weights" premise vanished, and routing became redundant); cross-encoder/LLM rerank is not adopted (high cost, Cohere v4 has already greatly reduced the need, poor ROI).

## 6. Full Data Flow (`GET /search`)

```
GET /search?q=&size=10&bm25_weight=(optional)
 ↓ [1] router.py validates parameters
 ↓ [2] service._resolve_bm25_weight: manual ?bm25_weight= > fixed 0.2
 ↓ [3] service._embed_query: mock→MOCK_QUERY_VECTOR; real→Cohere v4 aembed_query (search_query, L2-normalized)
 ↓ [4] repository.hybrid_msearch(vector, query, candidate_k=2×size)
 ↓     single msearch, concurrent: k-NN (faiss/innerproduct) + BM25 (smartcn multi_match)
 ↓ [5] fusion.min_max_score_fusion(knn_scored, bm25_scored, w_bm25, w_knn)
 ↓ [6] top-size → _id join _source → SearchResultItem DTO
 → SearchResponse(query, results, applied_bm25_weight, route_label)
   no results → results=[], HTTP 200
```

## 7. Architecture Diagram

```
                          GET /search?q=…&size=…&bm25_weight=(opt)
                                         │
                                ┌────────▼────────┐
                                │   router.py     │  validate q / size / bm25_weight
                                └────────┬────────┘
                                         │
                          ┌──────────────▼───────────────┐
                          │     service.py orchestration  │
                          │  _resolve_bm25_weight order:  │
                          │     manual > fixed 0.2        │
                          └───────────────┬──────────────┘
                                          │ w_bm25 decided
                                          ▼
                          ┌───────── two legs concurrent (one msearch round-trip) ─────────┐
                          │                                                      │
                 ┌────────▼─────────┐                              ┌─────────────▼────────┐
                 │  vector leg k-NN  │                              │  lexical leg BM25     │
                 │  Cohere v4 embed  │                              │  smartcn CJK tokenize │
                 │  1536/search_query│                              │  multi_match          │
                 │  faiss/innerproduct                             │  martName/feature/kw  │
                 │  candidate_k=2×size                             │  candidate_k=2×size    │
                 │  ✓semantic/symptom│                              │  ✓model/brand/exact    │
                 │  ~polarity(v4 much better)│                     │  ✗Trad per-char "腳" collision │
                 └────────┬─────────┘                              └─────────────┬────────┘
                          │ raw _score                                          │ raw _score
                          └──────────────────────┬───────────────────────────────┘
                                                 ▼
                              ┌──────────────────────────────────────┐
                              │  fusion.min_max_score_fusion          │
                              │  per-query min-max normalize then sum: │
                              │  w_knn·norm(knn) + w_bm25·norm(bm25)   │
                              └───────────────────┬──────────────────┘
                                                  ▼
                                top-size → _id join _source → DTO
                                                  ▼
                       SearchResponse(query, results[], applied_bm25_weight, route_label)

   ┌─ Data plane (indexes) ─────────────────────────────────────────────────────┐
   │  products_v5_cohere(prod) smartcn    + Cohere v4 1536       Chinese semantic much better ✅ │
   │  products_v1        smartcn          + Titan v2 1024(w/feat) former prod(228)     │
   │  products_v2        smartcn+Trad→Simp + same v1 vector       217 (worse)           │
   │  products_v3        t2s words+cjk_bigram + same v1 vector    212 (worst)           │
   │  products_v4_nofeat smartcn          + Titan(feature removed) 225 (fixed vector pollution) │
   │  invariant: query/doc same model, dim, normalize (Cohere v4/1536/L2/innerproduct)│
   └────────────────────────────────────────────────────────────────────────────┘

   Failure modes (see decision record): A polarity blindness (accepted) · B feature pollution (v4 feature-removed, fixed) · C CJK tokenization (kept v1)
```
