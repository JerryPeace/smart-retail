"""
Hybrid search accuracy evaluation script: three-way side-by-side comparison (hybrid / k-NN-only / BM25-only).

Background
----------
Phase 2 has implemented the `/search` hybrid endpoint (BM25 + k-NN, with RRF fusion on the application side).
This script evaluates whether hybrid is better than / no worse than a single method, reusing the Phase 1 golden set
and the LLM-judge scale from judge_search_relevance.py.

Inputs
------
- scripts/etl/golden_set_product_search.yaml (meta.status must be approved)
- A running app (mock OFF): GET http://localhost:8000/search?q=<query>&size=10
  to fetch hybrid top-10 (the app must first be started with ANALYZER_MOCK_MODE=false)
- OpenSearch http://localhost:9200 > index "products_v1" (k-NN-only / BM25-only queried directly)
- AWS Bedrock (profile=lab, region=ap-northeast-1)
  judge model: jp.anthropic.claude-opus-4-5 (Opus tier, overridable via the JUDGE_MODEL_ID environment variable)
  embed model: amazon.titan-embed-text-v2:0 (reuses verify_search_os.embed_query)

Output
------
out/search_eval_hybrid_{YYYYMMDD}.md (three side-by-side columns hybrid/knn/bm25 + two Summary verdicts)

Success criteria (design §10.3 / task 7.3)
---------------------------------------
(a) Global: total hybrid relevant count >= max(total knn-only relevant count, total bm25-only relevant count)
(b) Complementarity preserved:
    - Vector-strong queries (contextual, e.g. q11/q13) hybrid relevant count must not drop to zero (hybrid_rel >= 1)
    - BM25-strong queries (q04 ThinkPad) hybrid relevant count must not drop to zero (hybrid_rel >= 1)
Both met -> verdict ✅; if either fails -> honestly mark ❌, do not loosen the criteria.

Cost estimate
--------
- 15 queries × embed (Titan v2): 45 embeddings (k-NN path only)
- hybrid top-10 × 15 queries: hits the app /search endpoint (no Bedrock cost, the app embeds on its own)
- k-NN + BM25 queried directly against OpenSearch: 30 queries (no Bedrock cost)
- judge: 15 queries × on average ~17 unique products (deduplicated union of the three paths) ≈ 255 Opus calls
  Opus: input ~$15/M token, output ~$75/M token, ~300 tokens per call
  255 × 300 tokens ≈ 76,500 tokens ≈ estimated < $2 (Opus tier)
  Total cost is on the order of < $2, but it is still real Bedrock — "user consent must be obtained before running (safety.md §1)"
- mget to backfill hybrid-only doc source: at most 15 × SEARCH_K mget calls (0 Bedrock cost, OpenSearch only)

source_map artifact fix note
-----------------------------
The original implementation _build_source_map(knn_hits + bm25_hits) only covered the top-SEARCH_K docs of the two paths.
After hybrid fusion, if a doc enters the top-SEARCH_K but ranks deeper in both paths (rank > SEARCH_K),
then that doc's source is missing from source_map (blank product name/feature) -> the judge sees no information -> automatically judged irrelevant.
Fix: after fetching the three paths' hits in Phase 1, issue a single mget batch to backfill the mart_ids missing from source_map across the union of the three paths,
ensuring every judge item has complete product information (_enrich_source_map_with_mget).

safety disclosure requirement
---------------
This script hits real Bedrock (embed + judge); before running it "must" inform the user of the estimated cost and obtain consent
(safety.md §1: disclose cost before real Bedrock calls).
Before running, confirm:
  1. The app is started with ANALYZER_MOCK_MODE=false (localhost:8000 healthy)
  2. OpenSearch is online (localhost:9200 reachable)
  3. AWS lab credentials are valid (run bash scripts/refresh-lab-creds.sh if needed)
  4. User consent obtained (cost disclosed, on the order of <= $2)

gate
----
When meta.status != approved, exit 1 without making any external calls (enforced inside load_golden_set).

Usage
----
uv run python scripts/etl/judge_hybrid_search.py [YYYYMMDD]
When YYYYMMDD is omitted, DATE_PLACEHOLDER is used (does not rely on datetime.now(), aligned with the verify convention)
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# ---------- Constants ----------

APP_BASE_URL = "http://localhost:8000"
OS_HOST = "http://localhost:9200"
GOLDEN_SET_PATH = Path("scripts/etl/golden_set_product_search.yaml")
OUT_DIR = Path("out")
DATE_PLACEHOLDER = "YYYYMMDD"

BEDROCK_PROFILE = "lab"
BEDROCK_REGION = "ap-northeast-1"
# Opus-tier judge (high confidence, avoids drift across rounds); overridable via the JUDGE_MODEL_ID environment variable
JUDGE_MODEL_ID = os.environ.get(
    "JUDGE_MODEL_ID", "jp.anthropic.claude-opus-4-5-20251001-v1:0"
)

SEARCH_K = 10

# Success criterion (b): list of query IDs for complementarity preservation
# Vector-strong (contextual semantics): design §10.3 cites q11/q13 as representatives
VECTOR_STRONG_QUERIES = {"q11", "q13"}
# BM25-strong (exact lexical hits): design §10.3 cites q04 ThinkPad
BM25_STRONG_QUERIES = {"q04"}

# ---------- importlib loading of verify_search_os (reuse, don't rewrite) ----------
# Located via the absolute path of __file__ to avoid working-directory effects


def _load_verify_mod():
    """Safely load verify_search_os.py via importlib (without triggering its __main__ guard).

    Uses Path(__file__).parent to compute an absolute path, so the script works when run from any working directory.

    Returns:
        verify_search_os module object (with load_golden_set, embed_query,
        knn_search, bm25_search, INDEX_NAME and other attributes).
    """
    path = Path(__file__).parent / "verify_search_os.py"
    spec = importlib.util.spec_from_file_location("verify_search_os", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_judge_mod():
    """Safely load judge_search_relevance.py via importlib (reuse the judge engine).

    Follows the _load_verify_mod pattern, using Path(__file__).parent to compute an absolute path.

    Returns:
        judge_search_relevance module object (with _strip_html, _build_judge_prompt,
        _invoke_judge_single, _judge_batch, JudgeKey, JudgeCache,
        FEATURE_MAX_CHARS, JUDGE_WORKERS, RETRY_MAX and other attributes).
    """
    path = Path(__file__).parent / "judge_search_relevance.py"
    spec = importlib.util.spec_from_file_location("judge_search_relevance", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------- Hybrid search: hit the running app's /search endpoint ----------


def _fetch_hybrid_results(query_text: str, size: int = SEARCH_K) -> list[str]:
    """Hit the running app's GET /search and fetch the hybrid top-{size} mart_id list.

    Args:
        query_text: The search query string.
        size: Number of top-k results to fetch.

    Returns:
        list of mart_id (str), in descending RRF score order.
        Raises requests.exceptions.ConnectionError / httpx.ConnectError on connection failure.
    """
    import requests  # noqa: PLC0415  (stdlib / pyproject.toml dependency)

    url = f"{APP_BASE_URL}/search"
    resp = requests.get(url, params={"q": query_text, "size": size}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [item["mart_id"] for item in data.get("results", [])]


# ---------- Helper: build a martId→source lookup table from an OpenSearch hit list ----------


def _build_source_map(hits: list[dict]) -> dict[str, dict]:
    """Build a {martId: _source} lookup table from an OpenSearch hit list.

    Args:
        hits: list of OpenSearch hit dict (with _id, _source).

    Returns:
        {str(hit["_id"]): hit["_source"]} dict.
    """
    return {str(h["_id"]): (h.get("_source") or {}) for h in hits}


def _enrich_source_map_with_mget(
    os_client: object,
    index_name: str,
    source_map: dict[str, dict],
    all_mart_ids: set[str],
) -> None:
    """mget to backfill the _source of docs missing from source_map, filling it in place.

    hybrid top-k candidates may include docs that "only appear deep (rank k+1~) in one path";
    these docs are not in the top-k of knn_hits/bm25_hits, so source_map is blank for them.
    Blank product information means the judge sees no martName/feature -> automatically judged irrelevant (an artifact).

    This function issues a single mget batch request for the mart_ids not yet covered in source_map across the union of the three paths,
    backfilling the fields the judge needs, such as martName/feature/keyword/categoryLevelXName/brand/price.

    Idempotent design:
    - mart_ids that already have a source are not re-fetched (skipped even if some fields are empty, to avoid duplicate cost).
    - docs returned with found=False are not written (the original empty dict is preserved, not overwritten).

    Args:
        os_client: opensearchpy.OpenSearch instance (synchronous).
        index_name: OpenSearch index name (e.g. "products_v1").
        source_map: {mart_id: _source dict}, missing entries filled in place.
        all_mart_ids: the full set of mart_ids across the union of the three paths.
    """
    missing_ids = sorted(all_mart_ids - set(source_map))  # sorted for determinism
    if not missing_ids:
        return

    # mget batch fetch of _source (only the fields the judge needs, to reduce payload)
    resp = os_client.mget(  # type: ignore[attr-defined]
        index=index_name,
        body={
            "docs": [
                {
                    "_id": mid,
                    "_source": [
                        "martName", "feature", "keyword", "brand", "price",
                        "categoryLevel1Name", "categoryLevel2Name",
                    ],
                }
                for mid in missing_ids
            ]
        },
    )
    found_count = 0
    for doc in resp.get("docs", []):
        if doc.get("found"):
            source_map[str(doc["_id"])] = doc.get("_source") or {}
            found_count += 1

    if found_count < len(missing_ids):
        not_found = len(missing_ids) - found_count
        print(
            f"    [source mget] {found_count}/{len(missing_ids)} 補抓成功"
            f"（{not_found} 筆 OpenSearch 無記錄，保留空 source）"
        )
    else:
        print(f"    [source mget] 補抓 {found_count} 筆 hybrid-only doc source ✓")


# ---------- Helper: render a hit list as Markdown table rows ----------


def _render_hits_table(
    lines: list[str],
    hits: list[dict] | list[str],
    qid: str,
    judge_cache: dict,
    source_map: dict[str, dict],
    *,
    has_score: bool,
    title: str,
) -> None:
    """Render a hit list as a Markdown table, appending to lines.

    Args:
        lines:       output line buffer (appended in place).
        hits:        hit list; list[dict] (_id/_score/_source) when there is a score,
                     list[str] (mart_id) when there is no score (hybrid).
        qid:         query id, used to look up judge_cache.
        judge_cache: {(qid, mart_id): {"relevant": bool, "reason": str}}.
        source_map:  {mart_id: _source dict}, used to look up martName.
        has_score:   True -> the table has a score column (knn/bm25 path);
                     False -> no score column (hybrid path).
        title:       table section title (without ###).
    """
    lines.append(f"\n### {title}\n")
    if has_score:
        lines.append("| rank | martId | martName（前30字）| score | judge |")
        lines.append("|------|--------|------------------|-------|-------|")
    else:
        lines.append("| rank | martId | martName（前30字）| judge |")
        lines.append("|------|--------|------------------|-------|")

    for i, item in enumerate(hits):
        if has_score:
            hit = item  # type: ignore[assignment]
            mid = str(hit["_id"])
            src = hit.get("_source") or source_map.get(mid, {})
            score_str = f"{hit.get('_score', 0):.3f}"
        else:
            mid = item  # type: ignore[assignment]
            src = source_map.get(mid, {})
            score_str = None

        name = src.get("martName", "（無 source）")[:30]
        jr = judge_cache.get((qid, mid))
        relevant = (jr or {}).get("relevant", False)
        reason = (jr or {}).get("reason", "")[:15]
        rel_mark = "✓" if relevant else "✗"

        if score_str is not None:
            lines.append(f"| {i+1:4d} | {mid} | {name} | {score_str} | {rel_mark} {reason} |")
        else:
            lines.append(f"| {i+1:4d} | {mid} | {name} | {rel_mark} {reason} |")


# ---------- Main flow ----------


def main() -> None:
    """Main flow for the hybrid search three-way comparison (hybrid / k-NN-only / BM25-only).

    Preconditions:
    1. The app is started with ANALYZER_MOCK_MODE=false (localhost:8000)
    2. OpenSearch is online (localhost:9200)
    3. AWS lab credentials are valid
    4. User consent obtained (this script hits real Bedrock, cost on the order of < $2)
    """
    from opensearchpy import OpenSearch  # noqa: PLC0415

    # ── Initialization ──
    date_str = sys.argv[1] if len(sys.argv) > 1 else DATE_PLACEHOLDER
    out_path = OUT_DIR / f"search_eval_hybrid_{date_str}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Dynamically load modules (zero network, importlib only) ──
    verify_mod = _load_verify_mod()
    load_golden_set = verify_mod.load_golden_set
    embed_query = verify_mod.embed_query
    knn_search = verify_mod.knn_search
    bm25_search = verify_mod.bm25_search
    index_name = verify_mod.INDEX_NAME

    judge_mod = _load_judge_mod()
    JudgeCache = judge_mod.JudgeCache  # noqa: N806  (type alias, not a class)
    JudgeKey = judge_mod.JudgeKey  # noqa: N806

    # ── Gate: status check (load_golden_set exits 1 internally, makes no external calls) ──
    golden = load_golden_set(GOLDEN_SET_PATH)
    queries = golden.get("queries", [])
    print(f"Golden set loaded：{len(queries)} 條查詢")

    # ── OpenSearch client (with timeout/retry) ──
    os_client = OpenSearch(
        hosts=[OS_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True,
    )

    # ── Phase 1: for each query fetch three top-10 sets, building source_map at the same time (computed only once) ──
    # hybrid: hit the running app /search (mock OFF, the app does embed + RRF on its own)
    # k-NN-only: query OpenSearch directly (embed_query + knn_search)
    # BM25-only: query OpenSearch directly (bm25_search, no embed cost)
    print("\n[Phase 1] 取三路 top-10（hybrid / knn / bm25）…")
    query_results: list[dict] = []

    for q in queries:
        qid = q["id"]
        query_text = q["query"]
        category = q["category"]

        print(f"  [{qid}] query：{query_text!r}")

        # hybrid: hit the app /search, fetch the mart_id list (the app does embed + RRF, no local embed needed)
        print(f"    → hybrid（app /search）…")
        hybrid_mart_ids = _fetch_hybrid_results(query_text, size=SEARCH_K)

        # k-NN-only: local embed + query OpenSearch directly
        print(f"    → k-NN embed…")
        vector = embed_query(query_text)
        knn_hits = knn_search(os_client, vector, k=SEARCH_K)

        # BM25-only: query OpenSearch directly (zero Bedrock)
        bm25_hits = bm25_search(os_client, query_text, k=SEARCH_K)

        # source_map: the union of the three paths (hybrid ∪ knn ∪ bm25) all have complete _source, shared by Phase 2 and Phase 3.
        # 1. First build a base map from the two paths' hits (zero extra queries)
        source_map = _build_source_map(knn_hits + bm25_hits)
        # 2. mget to backfill the _source of hybrid-only docs (to avoid the judge seeing blank product names -> artifact)
        #    hybrid candidates may come from deep in the candidate window (rank 11+), not in the knn/bm25 top-10.
        all_mart_ids = (
            set(hybrid_mart_ids)
            | {str(h["_id"]) for h in knn_hits}
            | {str(h["_id"]) for h in bm25_hits}
        )
        _enrich_source_map_with_mget(os_client, index_name, source_map, all_mart_ids)

        query_results.append(
            {
                "qid": qid,
                "query_text": query_text,
                "category": category,
                "hybrid_mart_ids": hybrid_mart_ids,   # list[str], in descending RRF score order
                "knn_hits": knn_hits,                  # list[dict], with _id/_score/_source
                "bm25_hits": bm25_hits,                # list[dict], with _id/_score/_source
                "source_map": source_map,              # union of three paths martId→_source (backfilled via mget)
            }
        )

    # ── Phase 2: collect the union of the three paths, judge relevance with the same judge in one round (deduplicated via JudgeCache) ──
    # Each (query_id, mart_id) is judged only once, to avoid drift across paths.
    # Phase 1 already backfilled the _source of hybrid-only docs via mget, ensuring every judge item has complete product information.
    print("\n[Phase 2] 收集三路 unique 商品聯集，並發 judge…")

    judge_cache: dict = {}
    judge_items: list = []
    scheduled: set = set()

    for qr in query_results:
        qid = qr["qid"]
        query_text = qr["query_text"]
        knn_hits = qr["knn_hits"]
        bm25_hits = qr["bm25_hits"]
        hybrid_mart_ids = qr["hybrid_mart_ids"]
        source_map = qr["source_map"]  # reuse what Phase 1 already computed

        # union of the three paths (hybrid + knn + bm25), deduplicated by martId
        union_ids: dict[str, None] = {}
        for mid in hybrid_mart_ids:
            union_ids[mid] = None
        for h in knn_hits + bm25_hits:
            union_ids[str(h["_id"])] = None

        for mid in union_ids:
            key = (qid, mid)
            if key in scheduled:
                continue
            scheduled.add(key)
            src = source_map.get(mid, {})
            mart_name = src.get("martName", "")
            feature = src.get("feature", "")
            judge_items.append((key, query_text, mart_name, feature))

    total_pairs = len(judge_items)
    print(
        f"  三路聯集去重後需 judge {total_pairs} 個（query, 商品）對"
        f"（{judge_mod.JUDGE_WORKERS} workers）…"
    )
    judge_mod._judge_batch(judge_items, judge_cache)
    print(f"Judge 完成，快取 {len(judge_cache)} 筆。\n")

    # ── Phase 3: compute per-query metrics (hybrid_rel@10 / knn_rel@10 / bm25_rel@10) ──
    print("[Phase 3] 計算三路指標 & 輸出報告…")

    per_query_metrics: list[dict] = []
    lines: list[str] = []

    # Report header
    lines.append(f"# Search Eval Hybrid — {date_str}\n")
    lines.append(
        f"> 索引：`{index_name}`  \n"
        f"> Golden set：`{GOLDEN_SET_PATH}`  \n"
        f"> Judge 模型：`{JUDGE_MODEL_ID}`（Opus 級，同一輪同 judge 避免跨輪漂移）  \n"
        f"> 嵌入模型：`amazon.titan-embed-text-v2:0` dim=1024 normalize=true  \n"
        f"> Hybrid：app /search 端點 RRF（BM25+k-NN，mock OFF）  \n"
        f"> k-NN-only：直打 OpenSearch knn query  \n"
        f"> BM25-only：直打 OpenSearch multi_match  \n"
        f"> 相關性量尺：LLM binary（relevant true/false）  \n"
        f"> 成功標準(a)：hybrid 總相關數 ≥ max(knn-only 總相關數, bm25-only 總相關數)  \n"
        f"> 成功標準(b)：向量強項 query {sorted(VECTOR_STRONG_QUERIES)} 與 BM25 強項 query "
        f"{sorted(BM25_STRONG_QUERIES)} 的 hybrid_rel@10 均 ≥ 1  \n"
    )

    # Global aggregation counters
    total_hybrid_rel = 0
    total_knn_rel = 0
    total_bm25_rel = 0

    # Complementarity-preservation tracking: {qid: hybrid_rel_count}
    complement_check: dict[str, int] = {}

    for qr in query_results:
        qid = qr["qid"]
        query_text = qr["query_text"]
        category = qr["category"]
        hybrid_mart_ids = qr["hybrid_mart_ids"]
        knn_hits = qr["knn_hits"]
        bm25_hits = qr["bm25_hits"]
        source_map = qr["source_map"]  # reuse what Phase 1 already computed, no recompute

        # hybrid_rel@10: number of relevant items in the hybrid top-10
        hybrid_rel_count = sum(
            1 for mid in hybrid_mart_ids
            if judge_cache.get((qid, mid), {}).get("relevant", False)
        )
        # knn_rel@10: number of relevant items in the k-NN top-10
        knn_rel_count = sum(
            1 for h in knn_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
        )
        # bm25_rel@10: number of relevant items in the BM25 top-10
        bm25_rel_count = sum(
            1 for h in bm25_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
        )

        # Global accumulation
        total_hybrid_rel += hybrid_rel_count
        total_knn_rel += knn_rel_count
        total_bm25_rel += bm25_rel_count

        # Complementarity-preservation tracking
        if qid in VECTOR_STRONG_QUERIES or qid in BM25_STRONG_QUERIES:
            complement_check[qid] = hybrid_rel_count

        per_query_metrics.append(
            {
                "qid": qid,
                "query_text": query_text,
                "category": category,
                "hybrid_rel": hybrid_rel_count,
                "knn_rel": knn_rel_count,
                "bm25_rel": bm25_rel_count,
            }
        )

        # ── Per-query report block ──
        lines.append(f"\n## {qid}「{query_text}」 ({category})\n")
        lines.append(
            f"> 指標：**hybrid_rel@10={hybrid_rel_count}** | "
            f"**knn_rel@10={knn_rel_count}** | "
            f"**bm25_rel@10={bm25_rel_count}**\n"
        )

        _render_hits_table(
            lines, hybrid_mart_ids, qid, judge_cache, source_map,
            has_score=False,
            title=f"Hybrid top-{SEARCH_K}（app /search，RRF 融合分）",
        )
        _render_hits_table(
            lines, knn_hits, qid, judge_cache, source_map,
            has_score=True,
            title=f"k-NN-only top-{SEARCH_K}（直打 OpenSearch knn query）",
        )
        _render_hits_table(
            lines, bm25_hits, qid, judge_cache, source_map,
            has_score=True,
            title=f"BM25-only top-{SEARCH_K}（直打 OpenSearch multi_match）",
        )

    # ── Per-query summary table ──
    lines.append("\n---\n")
    lines.append("## 每 Query 指標彙總\n")
    lines.append(
        "| qid | category | hybrid_rel@10 | knn_rel@10 | bm25_rel@10 | hybrid 最優？ |"
    )
    lines.append("|-----|----------|:-------------:|:----------:|:-----------:|:------------:|")
    for m in per_query_metrics:
        best = max(m["knn_rel"], m["bm25_rel"])
        if m["hybrid_rel"] > best:
            best_str = "✅ 最優"
        elif m["hybrid_rel"] == best:
            best_str = "— 並列"
        else:
            best_str = "❌ 較差"
        lines.append(
            f"| {m['qid']} | {m['category']} | {m['hybrid_rel']} "
            f"| {m['knn_rel']} | {m['bm25_rel']} | {best_str} |"
        )

    # ── Summary ──
    lines.append("\n## Summary\n")

    lines.append("### 全局相關數\n")
    lines.append(
        f"| 方法 | 全局 rel@10 總計 |\n"
        f"|------|:--------------:|\n"
        f"| **hybrid** | **{total_hybrid_rel}** |\n"
        f"| k-NN-only | {total_knn_rel} |\n"
        f"| BM25-only | {total_bm25_rel} |\n"
    )

    # Success criterion (a): global hybrid >= max(knn, bm25)
    criterion_a_pass = total_hybrid_rel >= max(total_knn_rel, total_bm25_rel)
    lines.append("### 成功標準判定\n")
    lines.append(
        f"> **(a) 全局：hybrid 總相關數 ≥ max(knn-only, bm25-only)**  \n"
        f">  \n"
        f"> hybrid={total_hybrid_rel} vs max(knn={total_knn_rel}, bm25={total_bm25_rel})={max(total_knn_rel, total_bm25_rel)}  \n"
        f"> **{'達成 ✅' if criterion_a_pass else '未達 ❌'}**\n"
    )

    # Success criterion (b): complementarity preserved — hybrid does not drop to zero for either vector-strong or BM25-strong queries
    complement_results: list[str] = []
    criterion_b_pass = True
    for qid_check in sorted(VECTOR_STRONG_QUERIES | BM25_STRONG_QUERIES):
        h_rel = complement_check.get(qid_check, -1)
        kind = "向量強項" if qid_check in VECTOR_STRONG_QUERIES else "BM25 強項"
        if h_rel < 0:
            complement_results.append(f"  - {qid_check}（{kind}）：未出現於 golden set，跳過")
        elif h_rel == 0:
            complement_results.append(
                f"  - {qid_check}（{kind}）：hybrid_rel@10={h_rel} → **歸零 ❌**"
            )
            criterion_b_pass = False
        else:
            complement_results.append(
                f"  - {qid_check}（{kind}）：hybrid_rel@10={h_rel} → 保留 ✅"
            )

    lines.append(
        f"> **(b) 互補保留：向量強項 {sorted(VECTOR_STRONG_QUERIES)} 與 BM25 強項 "
        f"{sorted(BM25_STRONG_QUERIES)} 的 hybrid_rel@10 均 ≥ 1**  \n"
        ">\n"
    )
    for line in complement_results:
        lines.append(f"> {line}  \n")
    lines.append(f"> **{'達成 ✅' if criterion_b_pass else '未達 ❌'}**\n")

    # Overall verdict
    overall_pass = criterion_a_pass and criterion_b_pass
    lines.append(
        f"\n### 整體判定：**{'PASS ✅' if overall_pass else 'FAIL ❌'}**  \n"
        f"(a) 全局：{'✅' if criterion_a_pass else '❌'}  \n"
        f"(b) 互補保留：{'✅' if criterion_b_pass else '❌'}  \n"
    )

    lines.append(
        "\n> 誠實聲明：judge 結果如實呈現，不得事後調寬判定標準。  \n"
        f"> judge 模型：`{JUDGE_MODEL_ID}`（Opus 級，同一輪三路共用同一批 judge，避免跨輪漂移）。  \n"
        "> expected_mart_ids 僅供 golden set 設計參考，**非**相關性唯一標準答案。\n"
    )

    report = "\n".join(lines)
    out_path.write_text(report, encoding="utf-8")
    print(f"\n報告寫出：{out_path}")

    if not overall_pass:
        print(
            "[WARN] 成功標準未達成（如實回報，不調寬判定）：\n"
            f"  (a) 全局：{'OK' if criterion_a_pass else 'FAIL'}\n"
            f"  (b) 互補保留：{'OK' if criterion_b_pass else 'FAIL'}",
            file=sys.stderr,
        )
    else:
        print("[OK] 成功達標：hybrid 不劣於單一方法，互補保留通過。")


if __name__ == "__main__":
    main()
