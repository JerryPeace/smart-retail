# Search Tuning Decision Record (2026-06-13)

> **Status**: ✅ Closed; staying on the verified **products_v1** (current prod).
> **In one line**: That day we tried 4 optimizations (tuning w, traditional→simplified tokenization, bigram, soft tags); **all of them landed inside the golden set's noise band (±14-18), and bigram even got clearly worse**. The conclusion is that search has approached "the ceiling that a 50-item ruler can distinguish", so we **keep v1** and mark "intent reranking (A)" as **pending real query-distribution data before re-evaluating ROI**.

---

## 1. Background: Why we kicked off this investigation

- Phase 2 declared hybrid "79 > 76, target met" (15-item golden set). Two concerns remained unresolved: ① w_bm25=0.7 was swept and evaluated on the same 15 items (measuring yourself with your own ruler) ② the sample was too small.
- At the same time, the user observed in testing that the query "cold hands and feet in winter" returned "tripod / foot callus remover", which is obviously absurd.
- These two things triggered: first expand the ruler to do a statistical re-verification (the Phase 2c-1 groundwork), then follow the "cold hands and feet" thread all the way to root cause.

## 2. The ruler (the basis for all conclusions)

- **golden set v2**: 15→**50 items** (20 lexical + 30 non_overlap, spanning 5 major categories to correct v1's sampling bias of over-concentrating on health supplements; 11 items had classification contamination). `scripts/etl/golden_set_product_search.yaml` (status=approved).
- **Evaluation**: app `/search` (true min-max fusion) + directly hitting OpenSearch knn/bm25, Opus 4.8 judge on every query×doc pair, computing global rel@10.
- **Statistics**: paired bootstrap (B=10000, `scripts/etl/bootstrap_hybrid_margin.py`). **Key fact: the noise band of rel@10 on 50 items is about ±14-18 docs**—any "win by ten-something points" could be noise.

## 3. Four findings (each one is "intuition → slapped down by the data")

| # | Hypothesis | Approach | Data | Conclusion |
|---|------|------|------|------|
| **1** | Meeting 79>76 is real progress | Re-verify on 50 items + bootstrap | hybrid **228** vs bm25 **224**; margin +4, 95% CI **[−10,+18]**, P(hybrid>bm25)=69% | **Not significant**. "Target met" should be downgraded to "hybrid is no worse than BM25 + complementarity retained" |
| **2** | Lowering w_bm25 fixes cold hands and feet | 0.7→0.4 end-to-end | Cold hands and feet improved somewhat, but for "ThinkPad laptop" a stylus crowded out the real laptops | **Seesaw**: fixing one scenario hurts the brand. The whole w-sweep range 223–235 is within noise |
| **3** | smartcn's per-character splitting of traditional Chinese is the root cause; traditional→simplified fixes tokenization | Install stconvert, build products_v2, reindex | hybrid 217 / bm25 204; v1-vs-v2 hybrid **−11**, CI **[−38,+21]**, P=77%; **lexical +9 / non_overlap −20** | **Redistribution, overall a slight-negative wash**. The per-character split's "high recall" actually benefits most queries |
| **4** | bigram restores recall and gives us the best of both | Build products_v3 (t2s terms + cjk_bigram multi-field) | hybrid **212** / knn 214 / bm25 201; **hybrid < knn**; q04/q11/q13 all dropped to zero | **Worst**. bigram "laptop" floods accessories up and drowns ThinkPad (precision collapses) |

**Three-index total comparison (same 50-item ruler, same Opus 4.8 judge)**:

| Index | analyzer | hybrid | knn | bm25 | Verdict |
|------|----------|:---:|:---:|:---:|------|
| **products_v1** (current prod) | smartcn (per-character split of traditional) | **228** | 214 | 224 | ✅ Best |
| products_v2 | smartcn + traditional→simplified (t2s) | 217 | 212 | 204 | Slightly-negative wash |
| products_v3 | t2s terms + cjk_bigram multi-field | 212 | 214 | 201 | ❌ Worst |

> **Monotonic decline 228→217→212**: every "fix the tokenizer" made the whole thing worse. To fix one **rare visible pathology** (cold hands and feet), we made a chain of global changes, and the more we fixed the worse it got.

## 4. Why we keep v1 (core conclusion)

- **v1 is the best of the three indexes on the golden set**, and it is the verified state already shipped to prod in Phase 2.
- "Per-character splitting" looks bad, but its **high recall** is an advantage on most real queries (casting a wide net catches related items of the same kind); "cold hands and feet → tripod" is just a rare side effect of high recall.
- **Discipline**: don't ship changes that are unmeasured, or that got worse after measurement. v2/v3 got worse after measurement → don't ship.

## 5. The "cold hands and feet" root cause and the correct fix

- **Two layers of root cause**: ① BM25 side—smartcn splits traditional "cold hands and feet" into single characters `hand / foot / cold`, where "foot" collides with "tripod / foot callus remover" (already proven via `_analyze`). ② Vector side—the embedding associates "cold" with "refrigeration / cooling-sensation" cooling products (**intent polarity confusion**, which tokenization / bigram / tuning w cannot fix).
- **The correct fix is "surgery", not "global operation"**: a global tokenizer change touches all queries (proven to do more harm than good); only a **local rerank targeting intent polarity** avoids hurting the innocent.

## 6. Design and trade-offs of A / C (for future reference)

The full solution is one pipeline, two layers:
```
query →①intent classification ──────────────┐
                               ├→ ③rerank (boost same-intent / penalty opposite-intent)   ← A
candidate generation (tokenize/bigram) →②product intent tag ─┘
   ↑ C
```

- **C (candidate generation, bigram multi-field)**: already tried = **failed** (v3 worst overall, precision collapsed). A global change to candidate generation isn't worth it on this ruler.
- **A (intent-tag rerank)**: the POC verified that **hard signals work**—on v2 candidates, intent-tag reranking pulled "heated gloves" from #5 to #2 and pushed "cooling fan" from #2 down to #4. But:
  - **Soft signals don't work**: stuffing the tag into the embedding text is too weak (warming products only +0.003~0.017, cooling fan still #1). You must use a **hard signal** (filter/boost).
  - **A depends on the candidate pool**: on v1 (best overall), the hybrid candidates' k-NN leg actually **does** contain warming products (v1 knn diagnostics retrieve heated gloves / hand warmers); it's the BM25 "foot" noise outranking them during fusion → **A could perhaps do intent reranking directly on the v1 hybrid candidates, without touching the tokenizer**.
  - **A is a big effort**: a controlled intent vocabulary (~40-60 tags + opposite pairs) → LLM-label 26k (one-time, Haiku, structured output, sampled QA) → query intent classifier (lightweight, cacheable) → rerank layer → golden set measurement.
- **Nature of the labeling**: A's labeling isn't traditional manual labeling; it's semi-automatic—"humans define a controlled vocabulary + LLM labels 26k + humans spot-check QA".

## 7. Decision

1. **Stay on products_v1** (prod untouched). Keep the v2/v3 indexes on local OpenSearch as experimental assets; don't ship.
2. **Mark A (intent rerank) as "pending real query-distribution data before re-evaluating ROI"**. Conditions to trigger A:
   - Obtain real search logs and measure the actual share of "intent polarity confusion" queries (the cold-hands-and-feet type).
   - Only if the share is high enough (worth building a pipeline for it) do we start A, and **do the rerank directly on the v1 hybrid candidates** (don't repeat the v2/v3 global-tokenizer mistake).
   - Be prepared: A's gain may still be noise-level and require a larger ruler to detect.
3. **The real foundation for unlocking the next round = expand the real ruler**: take a week of real query logs, expand the golden set to several hundred real queries. Without this, all subsequent optimizations "can't be measured for true vs. false".

## 8. Assets left behind (paths)

| Type | Path |
|------|------|
| 50-item golden set (the ruler) | `scripts/etl/golden_set_product_search.yaml` |
| bootstrap significance tool | `scripts/etl/bootstrap_hybrid_margin.py` |
| w-sweep tool | `scripts/etl/wsweep_50q.py` |
| Groundwork decision report | `out/phase2c1_groundwork_20260613.md` |
| v1/v2/v3 evaluation reports | `out/search_eval_hybrid_50q_20260613.md`, `out/search_eval_hybrid_v2.md`, `out/search_eval_hybrid_v3.md` |
| bootstrap / w-sweep reports | `out/phase2c1_bootstrap_20260613.md`, `out/phase2c1_wsweep_50q_20260613.md` |
| Search testing UI | `ui/search.html` (pure HTML/JS) |
| This decision record | `docs/plans/search-tuning-decision-record-20260613.md` |

## 9. What was touched / not touched (for handoff)

- **Prod search restored to the v1 verified baseline**: BM25 in `src/recommender/search/repository.py` is back to its original state (`["martName","feature","keyword"]`); the prod index is still `products_v1`, `search_bm25_weight=0.7`.
- **Harmless changes kept**: `main.py` adds CORS (for ui/search.html); `verify_search_os.py`'s `OPENSEARCH_INDEX`/`BM25_FIELDS` can be overridden via env (default = original behavior); `docker/opensearch/Dockerfile` installs `analysis-stconvert` (currently unused, kept for evaluating A).
- **Local OpenSearch**: products_v1 (prod) + products_v2/v3 (experimental, can be deleted anytime).
- **Tests**: 141 passed, zero migrations.

---

## 10. (Future plan) Function-oriented labeling execution plan—focused, slice first then scale

> This is the focused, low-cost version of "A", refined from the user's "function-oriented" idea. **The goal is to fix the visible quality of "symptom-type queries" (e.g. cold hands and feet shouldn't return tripods / cooling products), not to raise the overall golden score** (proven to land in the noise). Recognize this positioning before starting work.

### 10.1 Core design
- **Function tag = what a product "is used to do"** (cooling fan = cooling, hand warmer = warming, lutein = eye protection). It bridges the gap where semantic search is weakest: the user types a **symptom/need** (cold hands and feet), the product describes a **function** (warming); the two are semantically close but lexically/vector-distance far apart.
- **The power is concentrated on function axes with "opposite polarity"** (warming↔cooling, weight-gain↔weight-loss, moisturizing↔oil-control…)—most of the gain comes from "penalizing the opposite". Functions without an opposite (protection, charging) degrade to a weak boost and aren't the point.
- **Rerank mechanism (hard signal, not stuffed into the embedding)**: classify the query's function → boost matching products, penalize opposite products. Already POC-verified (heated gloves #5→#2, cooling fan #2→#4). ⚠️ Soft signals (tag stuffed into the embedding text) are proven too weak; don't go there.

### 10.2 labeling pipeline (confidence routing / human-in-the-loop)
- **Don't use LLM self-reported confidence %** as the threshold—calibration is poor, it'll be confidently wrong, and Bedrock doesn't provide logprobs.
- **Use self-consistency (consistency across multiple samples) as the uncertainty signal**: label the same product with temperature>0 **3-5 times** →
  - All consistent → **auto-accept**.
  - ≥2 different tags appear, or it falls into "other" → **send to manual review**.
- Controlled function vocabulary + structured output (tags restricted to a whitelist). Cost: Haiku, the full 26k×5 is still very cheap (one-time).
- This compresses the manual volume down to just that small "low-consistency" subset, matching the user's reduction goal.

### 10.3 Execution order (discipline: slice first then scale, don't repeat the "build big then measure" mistake)
1. **First label only one polarity-axis slice**: e.g. the "fans·heaters" bucket (cooling vs heaters, a few hundred items), labeling warming/cooling.
2. **Build a query function classifier** (lightweight LLM, hot queries can be cached) + a rerank layer (boost/penalty).
3. **Measure on a small batch of real "symptom-type" queries**: confirm the target category is fixed and **other categories aren't collateral-damaged** (use the golden set's non-polarity queries as a control group).
4. **Scale gate**: only if the slice is verified effective do we scale to the full 26k + complete function vocabulary (multiple polarity axes).

### 10.4 Unchanging premises and risks
- **The overall golden score won't jump noticeably** (polarity-confusion queries are only 1-2 of the 50-item ruler); this is an investment in visible quality / brand perception, not an engineering win on an average metric.
- **Verifying "whether the whole got better" still requires expanding the real ruler** (Section 7)—without it, you can only claim "fixed a specific category", not "improved overall".
- Risk: a wrong query function classification → wrong rerank (possibly worse than doing nothing); a poorly defined polarity axis → wrongly penalizing legitimate cross-function products. So step 3's "control group not collateral-damaged" is a hard gate before scaling.

### 10.5 POC evidence already executed (2026-06-13, the "Do it" execution result)

We actually ran an end-to-end POC on the "fans·heaters" slice (279 items), with the conclusion below—**the mechanism is correct, but it can't escape the candidate-pool contradiction**:

- **labeling pipeline ✅ verified feasible**: self-consistency (3-round majority vote) labeled 279 items → **99% three-round-consistent auto-accept, only 2 items pending manual review**. This confirms the user's "high-confidence auto / low-confidence manual" is feasible, and that **the uncertainty signal should use "multi-round consistency", not "LLM self-reported %"**. Produced `out/slice_fan_heater_tags.json` (warming 63 / cooling 207 / neither 9).
- **Rerank mechanism ✅ works when slice products are in the candidate pool**:
  - v2 "cold hands and feet": hand warmer #6→**#2**, cooling fan penalized out of top-6. ✅
  - "beat the summer heat" (v1): the DYSON cooling fan boosted up to the front. ✅
- **❌ But on v1 (best overall index) it's completely ineffective for "cold hands and feet"**: v1's top-30 candidates are all tripods / foot callus removers / cups, with **no slice products at all** (the hand warmer was squeezed out of the candidate pool by BM25 "foot" noise) → the rerank has nothing to act on. **"The candidate pool is the gate" is nailed down by the same tag set producing opposite results on v1 vs v2**.
- **⚠️ Two new risk sources surfaced in testing**: ① the query classifier makes mistakes ("air fryer" was classified as "cooling"; this time it happened to do no harm because there were no slice products in the candidates); ② incomplete slice coverage (heated gloves are in another cat2, not in this slice, so they weren't boosted).
- **The fundamental contradiction (5th verification)**: fixing "cold hands and feet" requires v2/v3's candidate generation to pull warming products into the pool, but v2/v3 lower the overall score. **Function labeling cannot solve this tension**—it is a layer sitting on top of candidate generation, and if the candidates are wrong it is powerless.
- **Vocabulary design lesson—labels should be multi-label by nature; single-label was an over-simplification of this POC**: using single-label (warming XOR cooling XOR neither) for this slice was a wrong default restriction. The 2 "three-round wobbling" items (DIKE heating-cooling temperature-control fan, SONGEN four-season heating-and-cooling unit) were actually forced out by "only one allowed"—they are genuine dual-function products that under multi-label should naturally be labeled `[warming, cooling]` and wouldn't wobble. **The proper approach: labels are always multi-label, a product can carry multiple function tags; at rerank time, boost when "query function ∈ product tag set", and penalize only when "opposite function ∈ product tag set and query function is not in it"**. Dual-function products (containing both warming + cooling) are boosted for both query types and penalized for neither, naturally correct. For now we've labeled these 2 items `heating-cooling dual-use` as a transition (`out/slice_fan_heater_tags.json`).
- **Conclusion unchanged**: stay on v1. If function labeling is to be done, it must be evaluated together with candidate-generation changes, and **the overall gain still needs a real ruler to verify**—back to the foundation in Section 7.

---

## 11. (Future upgrade option) Unified multi-representation model (BGE-M3 class)—the upgraded version of Phase 2c's "swap the embedding"

> Refined from a user question: "Is there a model that, in a single inference, produces both 'vectorization' and 'keyword vs. semantic' results?" The answer is yes—a **multi-representation / unified embedding model**, exemplified by **BGE-M3**. This is the upgrade direction for Phase 2c's original "swap the embedding (Cohere vs Titan benchmark)" line: not just swapping the dense model, but swapping in a "dense + learned-sparse" unified model.

### 11.1 Core: one model, one inference, multiple representations
BGE-M3 outputs all of the following in a single forward pass:
1. **Dense vector** (semantic)—corresponds to what Titan does now.
2. **Sparse / lexical vector** (learned lexical, with term weights)—corresponds to "the keyword surface", but it's **learned**, smarter than smartcn's raw BM25.
3. ColBERT multi-vector (optional, for reranking).

### 11.2 Why it's more elegant than "Titan dense + smartcn BM25 + a brittle classifier"
- **Eliminates the query classifier**: no need to "first decide keyword/semantic, then pick weights" (Sections 6 and 10 of this record prove that classifier's dichotomy is too coarse and mostly over-engineering). Instead, "take the semantic representation + keyword representation at the same time, and each result decides for itself which side to lean on during fusion"—"keyword vs. semantic" becomes an **implicit result of fusion, not a misclassifiable upfront decision**.
- **learned-sparse may resolve today's CJK pain points**: it learns "term importance" rather than literal segmentation, potentially bypassing smartcn's "foot collision" from per-character splitting of traditional Chinese, and handling synonym gaps (sweep↔sweep-and-mop)—exactly the problems v2/v3 tried to fix but weren't worth it.
- If you still want an explicit "X% keyword" label: attach a tiny linear classification head on top of the dense vector (same inference → vector + classification, near-zero cost), but the classification head needs labeled training data.

### 11.3 Honest costs (a "next-generation architecture"-level investment, not a quick tweak)
1. **Swap the model**: Titan (dense only) + smartcn → BGE-M3 (or a Cohere / SPLADE class).
2. **Full re-embed + index change**: recompute dense+sparse for 26k; OpenSearch needs sparse fields (neural sparse / rank_features) + a fusion rewrite.
3. **Self-hosted inference**: BGE-M3 is open source, not on Bedrock, requiring a self-hosted GPU/CPU inference service (losing Titan's managed convenience + introducing new ops).
4. **Traditional Chinese quality unknown**: how multilingual learned-sparse actually performs in a traditional-Chinese e-commerce setting—**you can't assume it's better without testing**.
5. **No guarantee it fixes polarity**: the dense side may still be "topic≠polarity" (cold hands and feet↔refrigeration, the embedding physical limit this record repeatedly verified); learned-sparse + better fusion might help a bit, must be tested.

### 11.4 Positioning and order
- This is a **concrete upgrade candidate** for Phase 2c's "swap the embedding" line, and also a "next-generation retrieval architecture" experiment.
- **It must be sequenced after Section 7's "expand the real ruler"**: on the 50-item golden set (where all differences land in the noise band) you can't tell whether BGE-M3 is genuinely better or just old wine in a new bottle. Without a real ruler, the ROI of this migration can't be verified.
- Trigger conditions: ① a real query-log ruler is already in place ② confirmation that the current hybrid genuinely falls short on real traffic (rather than noise) → only then is it worth evaluating this migration cost.
