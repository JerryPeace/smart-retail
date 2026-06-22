"""
P1-5 verification script: run k-NN and BM25 side-by-side search against OpenSearch products_v1 to quantify the value of vector search.

Input   : scripts/etl/golden_set_product_search.yaml (meta.status must be approved)
          OpenSearch http://localhost:9200 > index "products_v1" (P1-4 embedding must be done first)
          AWS Bedrock (profile=lab, region=ap-northeast-1) amazon.titan-embed-text-v2:0
Output  : out/search_eval_{YYYYMMDD}.md (side-by-side comparison table + Summary)

Gate    : when meta.status != approved, exit 1 and refuse to run (programmatic enforcement, not reliance on self-discipline).
Safety  : about 20 query embeds; the cost is negligible, but it is still a real Bedrock call,
          so consent should be obtained together with the P1-4 cost disclosure (safety.md section 1).
Usage   : uv run python scripts/etl/verify_search_os.py [YYYYMMDD]
          when YYYYMMDD is omitted, DATE_PLACEHOLDER is used (so as not to depend on datetime.now() in the test environment)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------- Constants ----------

OS_HOST = "http://localhost:9200"
INDEX_NAME = os.environ.get("OPENSEARCH_INDEX", "products_v1")  # can be overridden to evaluate a different index (e.g. products_v2)
# BM25 fields can be overridden via env (v3 evaluation carries the .bigram multi-field); defaults to the old 3 fields (v1/v2 compatible)
BM25_FIELDS = os.environ.get(
    "BM25_FIELDS", "martName,feature,keyword"
).split(",")
GOLDEN_SET_PATH = Path("scripts/etl/golden_set_product_search.yaml")
OUT_DIR = Path("out")
BEDROCK_PROFILE = "lab"
BEDROCK_REGION = "ap-northeast-1"
BEDROCK_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIMENSIONS = 1024
SEARCH_K = 10
DATE_PLACEHOLDER = "YYYYMMDD"   # supplied by the user at run time, e.g. sys.argv[1] or an env var

# ---------- Pure function: golden set loader (importable by tests) ----------


def load_golden_set(path: Path | str = GOLDEN_SET_PATH) -> dict:
    """Load and validate the golden set YAML.

    Gate: meta.status must be 'approved'; otherwise print a message and sys.exit(1).
    This design is a programmatic gate, not reliance on the operator's self-discipline.

    Args:
        path: the golden set YAML path.

    Returns:
        the full YAML content dict (including meta + queries).

    Side-effects:
        when meta.status != 'approved', print a message and call sys.exit(1).
    """
    import yaml  # noqa: PLC0415  (pyyaml is already installed alongside opensearch-py or pre-commit)

    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    status = (data.get("meta") or {}).get("status", "")
    if status != "approved":
        print(
            f"[ERROR] golden set meta.status = '{status}'，需使用者審核後改為 'approved' 才可執行驗證。\n"
            f"  路徑：{path}\n"
            "  請逐條確認 query / expected_mart_ids，核可後修改 status 並填 approved_by / approved_at。",
            file=sys.stderr,
        )
        sys.exit(1)

    return data


# ---------- Reusable query functions (Phase 2's src/search_engine/service.py will lift these directly) ----------


def embed_query(text: str) -> list[float]:
    """Embed the query text into a 1024-dim vector (Titan v2, same model and params as the doc side).

    Args:
        text: the query string.

    Returns:
        a float list of length 1024.
    """
    import boto3  # noqa: PLC0415

    session = boto3.Session(profile_name=BEDROCK_PROFILE, region_name=BEDROCK_REGION)
    client = session.client("bedrock-runtime")
    body = json.dumps(
        {"inputText": text, "dimensions": EMBED_DIMENSIONS, "normalize": True}
    )
    response = client.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    return result["embedding"]


def knn_search(client: Any, vector: list[float], k: int = SEARCH_K) -> list[dict]:
    """Run a k-NN vector search against products_v1.

    Args:
        client: OpenSearch client instance.
        vector: the query vector (same dimension and normalization as the doc side).
        k: take the top-k results (default 10).

    Returns:
        list of hit dicts (including _id, _score, _source).
    """
    query = {
        "size": k,
        "query": {
            "knn": {
                "embedding": {
                    "vector": vector,
                    "k": k,
                }
            }
        },
    }
    resp = client.search(index=INDEX_NAME, body=query)
    return resp["hits"]["hits"]


def bm25_search(client: Any, query_text: str, k: int = SEARCH_K) -> list[dict]:
    """Run a BM25 multi_match search against products_v1 (martName / feature / keyword).

    Args:
        client: OpenSearch client instance.
        query_text: the search query string.
        k: take the top-k results (default 10).

    Returns:
        list of hit dicts (including _id, _score, _source).
    """
    query = {
        "size": k,
        "query": {
            "multi_match": {
                "query": query_text,
                "fields": BM25_FIELDS,
            }
        },
    }
    resp = client.search(index=INDEX_NAME, body=query)
    return resp["hits"]["hits"]


# ---------- Report helpers (inside the main guard, but simple enough to test) ----------


def _hit_at_k(hits: list[dict], expected_mart_ids: list[str]) -> int:
    """Compute hit@k: how many of expected_mart_ids land in the top-k hits."""
    found_ids = {str(h["_id"]) for h in hits}
    return sum(1 for mid in expected_mart_ids if str(mid) in found_ids)


def _format_hit_row(rank: int, knn_hit: dict | None, bm25_hit: dict | None) -> str:
    """Format one row of the side-by-side table."""
    def _fmt(hit: dict | None) -> str:
        if hit is None:
            return "—"
        mart_id = hit["_id"]
        name = (hit.get("_source") or {}).get("martName", "")[:20]
        score = hit.get("_score", 0)
        return f"{mart_id} {name} ({score:.3f})"

    return f"| {rank:4d} | {_fmt(knn_hit):<45} | {_fmt(bm25_hit):<45} |"


# ---------- Main flow ----------


def main() -> None:
    from opensearchpy import OpenSearch  # noqa: PLC0415

    date_str = sys.argv[1] if len(sys.argv) > 1 else DATE_PLACEHOLDER
    out_path = OUT_DIR / f"search_eval_{date_str}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Gate: status check (load_golden_set exits 1 internally)
    golden = load_golden_set(GOLDEN_SET_PATH)
    queries = golden.get("queries", [])
    print(f"Golden set loaded：{len(queries)} 條查詢")

    os_client = OpenSearch(hosts=[OS_HOST])

    lines: list[str] = []
    lines.append(f"# Search Eval — {date_str}\n")
    lines.append(
        f"> 索引：`{INDEX_NAME}`  \n"
        f"> Golden set：`{GOLDEN_SET_PATH}`  \n"
        f"> 模型：`{BEDROCK_MODEL_ID}` dimension={EMBED_DIMENSIONS} normalize=true\n"
    )

    non_overlap_total = 0
    non_overlap_vector_only_wins = 0
    lexical_total = 0
    lexical_bm25_hit_total = 0

    for q in queries:
        qid = q["id"]
        query_text = q["query"]
        category = q["category"]
        expected_ids = [str(m) for m in q.get("expected_mart_ids", [])]
        rationale = q.get("rationale", "")

        print(f"  [{qid}] 嵌入查詢：{query_text!r} …")
        vector = embed_query(query_text)
        knn_hits = knn_search(os_client, vector, k=SEARCH_K)
        bm25_hits = bm25_search(os_client, query_text, k=SEARCH_K)

        knn_hit = _hit_at_k(knn_hits, expected_ids)
        bm25_hit = _hit_at_k(bm25_hits, expected_ids)

        if category == "non_overlap":
            non_overlap_total += 1
            if knn_hit >= 1 and bm25_hit == 0:
                non_overlap_vector_only_wins += 1
        else:
            lexical_total += 1
            if bm25_hit >= 1:
                lexical_bm25_hit_total += 1

        lines.append(f"\n## {qid}「{query_text}」 ({category})\n")
        lines.append(f"> rationale: {rationale}  \n")
        lines.append(f"> expected_mart_ids: {expected_ids}\n")
        lines.append("")
        lines.append(f"| rank | 向量 top-{SEARCH_K}{' ' * 36} | BM25 top-{SEARCH_K}{' ' * 36} |")
        lines.append("|------|" + "-" * 47 + "|" + "-" * 47 + "|")

        max_len = max(len(knn_hits), len(bm25_hits), SEARCH_K)
        for i in range(max_len):
            kh = knn_hits[i] if i < len(knn_hits) else None
            bh = bm25_hits[i] if i < len(bm25_hits) else None
            lines.append(_format_hit_row(i + 1, kh, bh))

        lines.append("")
        lines.append(
            f"判定：vector hit@{SEARCH_K} = {knn_hit}/{len(expected_ids)}，"
            f"bm25 hit@{SEARCH_K} = {bm25_hit}/{len(expected_ids)}"
        )
        if category == "non_overlap":
            win = knn_hit >= 1 and bm25_hit == 0
            lines.append(f"  → {'**vector-only win ✅**' if win else 'not a clear vector win'}")

    # Category-contamination demo (always run in addition)
    lines.append("\n## 分類污染示範（category filter vs 純向量）\n")
    lines.append(
        "查詢：「保健食品」，加 categoryLevel1Name=「保健食品」filter vs 純向量搜尋\n"
    )
    demo_query = "保健食品"
    demo_vector = embed_query(demo_query)

    # Filtered search (may miss products whose category=brand name)
    filter_query = {
        "size": SEARCH_K,
        "query": {
            "bool": {
                "must": [
                    {
                        "knn": {
                            "embedding": {"vector": demo_vector, "k": SEARCH_K}
                        }
                    }
                ],
                "filter": [
                    {"term": {"categoryLevel1Name": "保健食品"}}
                ],
            }
        },
    }
    filter_hits = os_client.search(index=INDEX_NAME, body=filter_query)["hits"]["hits"]

    # Pure vector search (no filter)
    pure_knn_hits = knn_search(os_client, demo_vector, k=SEARCH_K)

    lines.append(f"| rank | filter+向量 top-{SEARCH_K}{' ' * 30} | 純向量 top-{SEARCH_K}{' ' * 31} |")
    lines.append("|------|" + "-" * 47 + "|" + "-" * 47 + "|")
    max_len = max(len(filter_hits), len(pure_knn_hits))
    for i in range(max_len):
        fh = filter_hits[i] if i < len(filter_hits) else None
        kh = pure_knn_hits[i] if i < len(pure_knn_hits) else None
        lines.append(_format_hit_row(i + 1, fh, kh))

    lines.append("")
    lines.append(
        f"> 說明：categoryLevel1Name='保健食品' filter 可能漏掉品牌館商品"
        f"（例如 category=品牌名 的靈芝王），而純向量搜尋透過語意相似度找到。"
    )

    # Summary
    SUCCESS_N = 3
    lines.append("\n## Summary\n")
    lines.append(
        f"- non_overlap 共 {non_overlap_total} 條："
        f"vector-only wins = {non_overlap_vector_only_wins} "
        f"（成功標準 ≥ {SUCCESS_N}：{'**達成 ✅**' if non_overlap_vector_only_wins >= SUCCESS_N else '**未達 ❌**'}）"
    )
    lines.append(
        f"- lexical_overlap 共 {lexical_total} 條："
        f"BM25 hit 率 = {lexical_bm25_hit_total}/{lexical_total} "
        f"（對照組健全性：兩邊都該行）"
    )
    lines.append(
        f"- 分類污染示範：filter+向量 找到 {len(filter_hits)} 筆 / "
        f"純向量 找到 {len(pure_knn_hits)} 筆"
    )

    report = "\n".join(lines)
    out_path.write_text(report, encoding="utf-8")
    print(f"\n報告寫出：{out_path}")

    if non_overlap_vector_only_wins < SUCCESS_N:
        print(
            f"[WARN] vector-only wins ({non_overlap_vector_only_wins}) < N={SUCCESS_N}，"
            "未達成功標準。如實回報，不調寬判定。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
