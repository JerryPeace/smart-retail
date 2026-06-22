"""
LLM-judge search relevance evaluation script: replaces the exact expected_mart_id hit scale with LLM-as-judge.

Background
----------
The original "hit@k exact match" scale is unfair to "multi-SKU-variant + generic semantic query" cases —
products that vector search finds as semantically relevant but that are not listed in expected_mart_ids are still judged as failures.
This script instead uses an LLM-judge to evaluate "relevance", with a binary verdict per (query, product):
  relevant: true/false + reason (<=15 chars).
expected_mart_ids is still kept in the output for reference, but is not the sole ground truth.

Inputs
------
- scripts/etl/golden_set_product_search.yaml (meta.status must be approved)
- OpenSearch http://localhost:9200 > index "products_v1" (26,014 docs already embedded)
- AWS Bedrock (profile=lab, region=ap-northeast-1)
  judge model: jp.anthropic.claude-haiku-4-5-20251001-v1:0
  embed model: amazon.titan-embed-text-v2:0 (reuses verify_search_os.embed_query)

Output
------
out/search_eval_judge_{YYYYMMDD}.md

Success criteria (from meta.success_threshold_N, defaults to 3)
------------------------------------------------
Of the 8 non_overlap queries, the number of "vector wins" (vec_rel@10 > bm25_rel@10) >= N.
A tie counts as a draw and does not count as a vector win.

judge call estimate
--------------
15 queries × on average ~17 unique products ≈ 255 calls (dict cache, each pair judged only once)
Haiku: input ~$0.0008/K token, ~220 tokens per call
255 × 220 tokens ≈ 56,100 tokens ≈ $0.045 — very low cost

safety
----
This script hits real Bedrock (judge + embed); user consent must be obtained before running (safety.md §1).

Usage
----
uv run python scripts/etl/judge_search_relevance.py [YYYYMMDD]
When YYYYMMDD is omitted, DATE_PLACEHOLDER is used (does not rely on datetime.now(), aligned with the verify convention)
"""

from __future__ import annotations

import html
import importlib.util
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# ---------- Constants ----------

OS_HOST = "http://localhost:9200"
GOLDEN_SET_PATH = Path("scripts/etl/golden_set_product_search.yaml")
OUT_DIR = Path("out")
DATE_PLACEHOLDER = "YYYYMMDD"

BEDROCK_PROFILE = "lab"
BEDROCK_REGION = "ap-northeast-1"
# Overridable via the JUDGE_MODEL_ID environment variable (e.g. jp.anthropic.claude-opus-4-8 for a high-confidence re-judge)
JUDGE_MODEL_ID = os.environ.get(
    "JUDGE_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
)

SEARCH_K = 10
FEATURE_MAX_CHARS = 200   # feature truncation length in the judge prompt
JUDGE_WORKERS = 8          # number of concurrent judge calls in ThreadPoolExecutor
RETRY_MAX = 6              # max retries for exponential backoff

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


# ---------- HTML cleanup ----------


def _strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities, returning plain text."""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ---------- LLM Judge ----------


def _build_judge_prompt(query: str, mart_name: str, feature_snippet: str) -> str:
    """Build the judge prompt, requiring it to return only JSON {"relevant": bool, "reason": str}.

    Design principles (ai-prompting.md):
    - Give the "why" so the model judges correctly in borderline cases (semantic relevance suffices)
    - Give counter-examples to prevent misjudgment
    - Require JSON-only to avoid wrapper text interfering with parsing
    - Limit reason to 15 chars, to save output tokens

    Args:
        query: The user's search query.
        mart_name: The product name (martName).
        feature_snippet: The first 200 chars of the product feature (HTML already stripped).

    Returns:
        The complete prompt string.
    """
    return f"""你是電商搜尋相關性評審。判斷商品對查詢是否「相關」。

查詢（buyer 的需求語句）：{query}

商品：
- martName: {mart_name}
- feature（前200字）: {feature_snippet}

判斷標準（relevant: true = 相關）：
- 商品能滿足查詢需求，即使用詞不同（語意相關即可）
- 商品是查詢所找的東西的同類、替代品、或直接解決方案

反例（判 relevant: false）：
- 商品類別完全無關（查耳機 → 商品是掃地機）
- 商品僅因品牌名出現在索引，但功能毫無關聯

只回傳 JSON，不加任何其他說明：
{{"relevant": true 或 false, "reason": "最多15字繁中理由"}}"""


def _invoke_judge_single(
    session: Any,
    query: str,
    mart_name: str,
    feature: str,
) -> dict[str, Any]:
    """Call the judge LLM for a single (query, product), with exponential-backoff retries.

    Args:
        session: boto3.Session (created per-thread, to avoid sharing across threads).
        query: The search query.
        mart_name: The product name.
        feature: The raw product feature text (this function strips + truncates it internally).

    Returns:
        {"relevant": bool, "reason": str}
        Returns {"relevant": False, "reason": "解析失敗"} on parse failure.

    Raises:
        ExpiredTokenException: credentials expired; handled uniformly by the caller, which prints refresh guidance.
        RuntimeError: still failing after exceeding the max retry count.
    """
    feature_snippet = _strip_html(feature)[:FEATURE_MAX_CHARS]
    prompt_text = _build_judge_prompt(query, mart_name, feature_snippet)

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": prompt_text}],
        }
    )

    client = session.client("bedrock-runtime")
    last_exc: Exception | None = None

    for attempt in range(RETRY_MAX):
        try:
            resp = client.invoke_model(
                modelId=JUDGE_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            text = json.loads(resp["body"].read())["content"][0]["text"].strip()
            # Parse JSON (handling the case where the LLM may wrap it in a markdown code fence)
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                parsed = json.loads(m.group())
                return {
                    "relevant": bool(parsed.get("relevant", False)),
                    "reason": str(parsed.get("reason", ""))[:20],
                }
            return {"relevant": False, "reason": "解析失敗"}

        except Exception as exc:  # noqa: BLE001
            exc_str = str(exc)
            exc_name = type(exc).__name__

            # Credentials expired -> raise directly, let the caller handle it uniformly
            if "ExpiredToken" in exc_name or "ExpiredTokenException" in exc_str:
                raise

            # Throttling / 5xx -> exponential backoff
            retryable = (
                "ThrottlingException" in exc_str
                or "TooManyRequestsException" in exc_str
                or "ServiceUnavailable" in exc_str
                or "InternalServerError" in exc_str
            )
            if retryable and attempt < RETRY_MAX - 1:
                wait = 2 ** attempt
                print(
                    f"    [retry {attempt+1}/{RETRY_MAX}] {exc_name}，"
                    f"{wait}s 後重試…",
                    file=sys.stderr,
                )
                time.sleep(wait)
                last_exc = exc
                continue

            # Other exceptions -> do not retry, return a failure result
            return {"relevant": False, "reason": f"呼叫異常:{exc_name}"}

    raise RuntimeError(f"超過最大重試次數 {RETRY_MAX}") from last_exc


# ---------- Concurrent judge ----------

JudgeKey = tuple[str, str]  # (query_id, martId)
JudgeCache = dict[JudgeKey, dict[str, Any]]


def _judge_batch(
    items: list[tuple[JudgeKey, str, str, str]],
    cache: JudgeCache,
) -> None:
    """Concurrently judge a batch of (query_id, martId, query_text, mart_name, feature).

    Each JudgeKey is judged only once (cache deduplication).
    On ExpiredToken, print refresh guidance, salvage completed results, then sys.exit(1).

    Args:
        items: list of (key, query_text, mart_name, feature).
        cache: completed cache (updated in place, key = JudgeKey).

    Side-effects:
        Updates cache in place.
        sys.exit(1) on ExpiredToken.
    """
    import boto3  # noqa: PLC0415

    # Filter out already-cached items (don't re-judge)
    pending = [item for item in items if item[0] not in cache]
    if not pending:
        return

    expired_flag = {"hit": False}

    def _worker(item: tuple[JudgeKey, str, str, str]) -> tuple[JudgeKey, dict]:
        key, query_text, mart_name, feature = item
        # per-thread session (to avoid sharing the client across threads)
        session = boto3.Session(profile_name=BEDROCK_PROFILE, region_name=BEDROCK_REGION)
        result = _invoke_judge_single(session, query_text, mart_name, feature)
        return key, result

    total = len(pending)
    done_count = 0

    with ThreadPoolExecutor(max_workers=JUDGE_WORKERS) as executor:
        futures = {executor.submit(_worker, item): item for item in pending}
        for future in as_completed(futures):
            try:
                key, result = future.result()
                cache[key] = result
                done_count += 1
                if done_count % 20 == 0 or done_count == total:
                    print(f"  judge 進度：{done_count}/{total}")
            except Exception as exc:  # noqa: BLE001
                exc_str = str(exc)
                if "ExpiredToken" in type(exc).__name__ or "ExpiredToken" in exc_str:
                    if not expired_flag["hit"]:
                        expired_flag["hit"] = True
                        print(
                            "\n[ERROR] AWS 憑證已過期（ExpiredTokenException）。\n"
                            "  請執行以下指令刷新憑證後重試：\n"
                            "    bash scripts/refresh-lab-creds.sh\n"
                            f"  已 salvage {len(cache)} 筆 judge 結果（快取中），"
                            "重跑時不會重複計費。",
                            file=sys.stderr,
                        )
                    for f in futures:
                        f.cancel()
                else:
                    key = futures[future][0]
                    print(
                        f"  [WARN] judge 失敗 {key}: {exc}，標記為 not relevant",
                        file=sys.stderr,
                    )
                    cache[key] = {"relevant": False, "reason": f"judge 錯誤:{type(exc).__name__}"}

    if expired_flag["hit"]:
        sys.exit(1)


# ---------- Main flow ----------


def main() -> None:
    """Main flow for LLM-judge search relevance evaluation."""
    from opensearchpy import OpenSearch  # noqa: PLC0415

    # ── Initialization ──
    date_str = sys.argv[1] if len(sys.argv) > 1 else DATE_PLACEHOLDER
    out_path = OUT_DIR / f"search_eval_judge_{date_str}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Dynamically load verify_search_os to reuse its functions ──
    verify_mod = _load_verify_mod()
    load_golden_set = verify_mod.load_golden_set
    embed_query = verify_mod.embed_query
    knn_search = verify_mod.knn_search
    bm25_search = verify_mod.bm25_search
    index_name = verify_mod.INDEX_NAME

    # ── Gate: status check (load_golden_set exits 1 internally) ──
    golden = load_golden_set(GOLDEN_SET_PATH)
    queries = golden.get("queries", [])
    meta = golden.get("meta", {})
    success_n = int(meta.get("success_threshold_N", 3))
    print(f"Golden set loaded：{len(queries)} 條查詢  成功門檻 N={success_n}")

    # ── OpenSearch client (with timeout/retry) ──
    os_client = OpenSearch(
        hosts=[OS_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True,
    )

    # ── Phase 1: embed queries & fetch vector/BM25 top-10 ──
    print("\n[Phase 1] 嵌入查詢 & 取 top-10…")
    query_results: list[dict] = []

    for q in queries:
        qid = q["id"]
        query_text = q["query"]
        category = q["category"]
        expected_ids = [str(m) for m in q.get("expected_mart_ids", [])]

        print(f"  [{qid}] 嵌入：{query_text!r}…")
        vector = embed_query(query_text)
        vec_hits = knn_search(os_client, vector, k=SEARCH_K)
        bm25_hits = bm25_search(os_client, query_text, k=SEARCH_K)

        query_results.append(
            {
                "qid": qid,
                "query_text": query_text,
                "category": category,
                "expected_ids": expected_ids,
                "rationale": q.get("rationale", ""),
                "vec_hits": vec_hits,
                "bm25_hits": bm25_hits,
            }
        )

    # ── Phase 2: collect the deduplicated union of all (query_id, martId) pairs that need judging ──
    judge_cache: JudgeCache = {}
    judge_items: list[tuple[JudgeKey, str, str, str]] = []
    scheduled: set[JudgeKey] = set()

    for qr in query_results:
        qid = qr["qid"]
        query_text = qr["query_text"]
        # union (vec + bm25), deduplicated by martId
        union_hits: dict[str, dict] = {}
        for h in qr["vec_hits"] + qr["bm25_hits"]:
            mid = str(h["_id"])
            if mid not in union_hits:
                union_hits[mid] = h

        for mid, hit in union_hits.items():
            key: JudgeKey = (qid, mid)
            if key in scheduled:
                continue
            scheduled.add(key)
            src = hit.get("_source") or {}
            mart_name = src.get("martName", "")
            feature = src.get("feature", "")
            judge_items.append((key, query_text, mart_name, feature))

    total_pairs = len(judge_items)
    print(f"\n[Phase 2] 並發 judge {total_pairs} 個（query, 商品）對（{JUDGE_WORKERS} workers）…")
    _judge_batch(judge_items, judge_cache)
    print(f"Judge 完成，快取 {len(judge_cache)} 筆。\n")

    # ── Phase 3: compute per-query metrics ──
    print("[Phase 3] 計算指標 & 輸出報告…")

    per_query_metrics: list[dict] = []
    lines: list[str] = []

    # Report header
    lines.append(f"# Search Eval (LLM-Judge) — {date_str}\n")
    lines.append(
        f"> 索引：`{index_name}`  \n"
        f"> Golden set：`{GOLDEN_SET_PATH}`  \n"
        f"> Judge 模型：`{JUDGE_MODEL_ID}`  \n"
        f"> 嵌入模型：`amazon.titan-embed-text-v2:0` dim=1024 normalize=true  \n"
        f"> 相關性量尺：LLM binary（relevant true/false）——**非 expected_mart_ids 精確命中**  \n"
        f"> 成功標準：non_overlap 8 條中，`vec_rel@10 > bm25_rel@10` 的 query 數 ≥ {success_n}  \n"
        f"> ★ = vec_only_rel（向量找到、BM25 top-10 未出現且被判 relevant）\n"
    )

    # Aggregation counters
    non_overlap_count = 0
    non_overlap_vec_rel_sum = 0
    non_overlap_bm25_rel_sum = 0
    non_overlap_vec_wins = 0

    lexical_count = 0
    lexical_vec_rel_sum = 0
    lexical_bm25_rel_sum = 0

    for qr in query_results:
        qid = qr["qid"]
        query_text = qr["query_text"]
        category = qr["category"]
        vec_hits: list[dict] = qr["vec_hits"]
        bm25_hits: list[dict] = qr["bm25_hits"]

        vec_ids = {str(h["_id"]) for h in vec_hits}
        bm25_ids = {str(h["_id"]) for h in bm25_hits}

        # vec_rel@10: number of relevant items in the vector top-10
        vec_rel_count = sum(
            1 for h in vec_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
        )
        # bm25_rel@10: number of relevant items in the BM25 top-10
        bm25_rel_count = sum(
            1 for h in bm25_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
        )
        # vec_only_rel: in vec top-10 and relevant, but not in bm25 top-10
        vec_only_rel_ids = {
            str(h["_id"])
            for h in vec_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
            and str(h["_id"]) not in bm25_ids
        }
        # bm25_only_rel: in bm25 top-10 and relevant, but not in vec top-10
        bm25_only_rel_ids = {
            str(h["_id"])
            for h in bm25_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
            and str(h["_id"]) not in vec_ids
        }

        vec_only_rel_count = len(vec_only_rel_ids)
        bm25_only_rel_count = len(bm25_only_rel_ids)

        per_query_metrics.append(
            {
                "qid": qid,
                "query_text": query_text,
                "category": category,
                "vec_rel": vec_rel_count,
                "bm25_rel": bm25_rel_count,
                "vec_only_rel": vec_only_rel_count,
                "bm25_only_rel": bm25_only_rel_count,
            }
        )

        # Aggregate
        if category == "non_overlap":
            non_overlap_count += 1
            non_overlap_vec_rel_sum += vec_rel_count
            non_overlap_bm25_rel_sum += bm25_rel_count
            if vec_rel_count > bm25_rel_count:
                non_overlap_vec_wins += 1
        else:
            lexical_count += 1
            lexical_vec_rel_sum += vec_rel_count
            lexical_bm25_rel_sum += bm25_rel_count

        # ── Per-query report block ──
        lines.append(f"\n## {qid}「{query_text}」 ({category})\n")
        lines.append(f"> rationale: {qr['rationale']}  \n")
        lines.append(f"> expected_mart_ids（僅供參考）: {qr['expected_ids']}\n")
        lines.append(
            f"> 指標：**vec_rel@10={vec_rel_count}** | **bm25_rel@10={bm25_rel_count}** | "
            f"vec_only_rel={vec_only_rel_count} | bm25_only_rel={bm25_only_rel_count}\n"
        )

        # Vector top-10 table
        lines.append(f"### 向量 top-{SEARCH_K}\n")
        lines.append("| rank | martId | martName（前30字）| score | judge |")
        lines.append("|------|--------|------------------|-------|-------|")
        for i, hit in enumerate(vec_hits):
            mid = str(hit["_id"])
            src = hit.get("_source") or {}
            name = src.get("martName", "")[:30]
            score = hit.get("_score", 0)
            jr = judge_cache.get((qid, mid))
            relevant = (jr or {}).get("relevant", False)
            reason = (jr or {}).get("reason", "")[:15]
            rel_mark = "✓" if relevant else "✗"
            star = " ★" if mid in vec_only_rel_ids else ""
            lines.append(
                f"| {i+1:4d} | {mid} | {name} | {score:.3f} | {rel_mark} {reason}{star} |"
            )

        # BM25 top-10 table
        lines.append(f"\n### BM25 top-{SEARCH_K}\n")
        lines.append("| rank | martId | martName（前30字）| score | judge |")
        lines.append("|------|--------|------------------|-------|-------|")
        for i, hit in enumerate(bm25_hits):
            mid = str(hit["_id"])
            src = hit.get("_source") or {}
            name = src.get("martName", "")[:30]
            score = hit.get("_score", 0)
            jr = judge_cache.get((qid, mid))
            relevant = (jr or {}).get("relevant", False)
            reason = (jr or {}).get("reason", "")[:15]
            rel_mark = "✓" if relevant else "✗"
            star = " ★" if mid in bm25_only_rel_ids else ""
            lines.append(
                f"| {i+1:4d} | {mid} | {name} | {score:.3f} | {rel_mark} {reason}{star} |"
            )

        # non_overlap verdict
        if category == "non_overlap":
            if vec_rel_count > bm25_rel_count:
                verdict = "**向量勝 ✅**"
            elif vec_rel_count == bm25_rel_count:
                verdict = "平手（不計入向量勝）"
            else:
                verdict = "向量負 ❌"
            lines.append(
                f"\n判定：{verdict}（vec_rel={vec_rel_count} vs bm25_rel={bm25_rel_count}）\n"
            )

    # ── Per-query summary table ──
    lines.append("\n---\n")
    lines.append("## 每 Query 指標彙總\n")
    lines.append(
        "| qid | category | vec_rel@10 | bm25_rel@10 | vec_only_rel | bm25_only_rel | 向量勝？ |"
    )
    lines.append("|-----|----------|:----------:|:-----------:|:------------:|:-------------:|:-------:|")
    for m in per_query_metrics:
        if m["category"] == "non_overlap":
            if m["vec_rel"] > m["bm25_rel"]:
                win_str = "✅"
            elif m["vec_rel"] == m["bm25_rel"]:
                win_str = "—"
            else:
                win_str = "❌"
        else:
            win_str = "（對照組）"
        lines.append(
            f"| {m['qid']} | {m['category']} | {m['vec_rel']} | {m['bm25_rel']} "
            f"| {m['vec_only_rel']} | {m['bm25_only_rel']} | {win_str} |"
        )

    # ── Summary ──
    lines.append("\n## Summary\n")

    mean_vec_non = non_overlap_vec_rel_sum / non_overlap_count if non_overlap_count else 0.0
    mean_bm25_non = non_overlap_bm25_rel_sum / non_overlap_count if non_overlap_count else 0.0
    mean_vec_lex = lexical_vec_rel_sum / lexical_count if lexical_count else 0.0
    mean_bm25_lex = lexical_bm25_rel_sum / lexical_count if lexical_count else 0.0

    total_vec_only = sum(m["vec_only_rel"] for m in per_query_metrics)
    total_bm25_only = sum(m["bm25_only_rel"] for m in per_query_metrics)

    lines.append(
        f"| 類別 | query 數 | mean vec_rel@10 | mean bm25_rel@10 |\n"
        f"|------|:---:|:---:|:---:|\n"
        f"| non_overlap | {non_overlap_count} | {mean_vec_non:.2f} | {mean_bm25_non:.2f} |\n"
        f"| lexical_overlap | {lexical_count} | {mean_vec_lex:.2f} | {mean_bm25_lex:.2f} |\n"
    )
    lines.append(
        f"- 全局 vec_only_rel 總計：**{total_vec_only}** 筆（向量獨有且 relevant）\n"
        f"- 全局 bm25_only_rel 總計：**{total_bm25_only}** 筆（BM25 獨有且 relevant）\n"
    )

    # Success criteria verdict
    success = non_overlap_vec_wins >= success_n
    lines.append("### 成功標準判定\n")
    lines.append(
        f"> 成功標準定義：non_overlap {non_overlap_count} 條中，  \n"
        f"> `vec_rel@10 > bm25_rel@10`（「向量勝」）的 query 數 ≥ {success_n}。  \n"
        f"> 相同時算平手，不計入向量勝。  \n"
        f">  \n"
        f"> 向量勝 query 數 = **{non_overlap_vec_wins}** / {non_overlap_count}  \n"
        f"> {'**達成 ✅**' if success else '**未達 ❌**'}\n"
    )

    lines.append(
        "\n> 誠實聲明：judge 結果如實呈現，不得事後調寬判定標準。  \n"
        f"> judge 模型：`{JUDGE_MODEL_ID}`，reason 為二元判定簡短說明。  \n"
        "> expected_mart_ids 僅供參考，**非**相關性唯一標準答案。\n"
    )

    report = "\n".join(lines)
    out_path.write_text(report, encoding="utf-8")
    print(f"\n報告寫出：{out_path}")

    if not success:
        print(
            f"[WARN] non_overlap 向量勝 ({non_overlap_vec_wins}) < N={success_n}，"
            "未達成功標準。如實回報，不調寬判定。",
            file=sys.stderr,
        )
    else:
        print(
            f"[OK] 成功達標：non_overlap 向量勝 = {non_overlap_vec_wins} ≥ {success_n}"
        )


if __name__ == "__main__":
    main()
