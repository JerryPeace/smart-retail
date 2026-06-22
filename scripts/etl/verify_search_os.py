"""
P1-5 驗證腳本：對 OpenSearch products_v1 執行 k-NN 與 BM25 並排搜尋，量化向量搜尋價值.

輸入  : scripts/etl/golden_set_product_search.yaml（meta.status 必須為 approved）
        OpenSearch http://localhost:9200 > index "products_v1"（需先完成 P1-4 嵌入）
        AWS Bedrock (profile=lab, region=ap-northeast-1) amazon.titan-embed-text-v2:0
輸出  : out/search_eval_{YYYYMMDD}.md（並排比較表 + Summary）

Gate  : meta.status != approved 時 exit 1 拒跑（程式化強制，不靠自覺）。
安全  : 約 20 次 query 嵌入，成本忽略不計，但仍是真 Bedrock 呼叫，
        需與 P1-4 的花費告知一併取得同意（safety.md §1）。
用法  : uv run python scripts/etl/verify_search_os.py [YYYYMMDD]
        YYYYMMDD 省略時使用 DATE_PLACEHOLDER（在測試環境下不依賴 datetime.now()）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------- 常數 ----------

OS_HOST = "http://localhost:9200"
INDEX_NAME = os.environ.get("OPENSEARCH_INDEX", "products_v1")  # 可覆寫評估不同索引(如 products_v2)
# BM25 欄位可由 env 覆寫（v3 評估時帶 .bigram 多欄位）；預設舊 3 欄(v1/v2 相容)
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
DATE_PLACEHOLDER = "YYYYMMDD"   # 使用者執行時帶入，e.g. sys.argv[1] 或 env var

# ---------- 純函式：golden set loader（可被測試 import）----------


def load_golden_set(path: Path | str = GOLDEN_SET_PATH) -> dict:
    """載入並驗證 golden set YAML.

    Gate：meta.status 必須為 'approved'，否則 print 提示並 sys.exit(1)。
    此設計是程式化 gate，不靠執行者自覺。

    Args:
        path: golden set YAML 路徑。

    Returns:
        完整 YAML 內容 dict（包含 meta + queries）。

    Side-effects:
        meta.status != 'approved' 時印提示並呼叫 sys.exit(1)。
    """
    import yaml  # noqa: PLC0415  (pyyaml 已隨 opensearch-py 或 pre-commit 安裝)

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


# ---------- 可重用查詢函式（Phase 2 的 src/search_engine/service.py 會直接 lift）----------


def embed_query(text: str) -> list[float]:
    """將查詢文字嵌入為 1024 維向量（Titan v2，與 doc 端同模型同參數）.

    Args:
        text: 查詢字串。

    Returns:
        長度 1024 的 float list。
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
    """對 products_v1 執行 k-NN 向量搜尋.

    Args:
        client: OpenSearch client instance。
        vector: 查詢向量（與 doc 端同維度同 normalize）。
        k: 取 top-k 筆（預設 10）。

    Returns:
        list of hit dict（含 _id、_score、_source）。
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
    """對 products_v1 執行 BM25 multi_match 搜尋（martName / feature / keyword）.

    Args:
        client: OpenSearch client instance。
        query_text: 搜尋查詢字串。
        k: 取 top-k 筆（預設 10）。

    Returns:
        list of hit dict（含 _id、_score、_source）。
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


# ---------- 報告輔助（main guard 內，但邏輯簡單可測）----------


def _hit_at_k(hits: list[dict], expected_mart_ids: list[str]) -> int:
    """計算 hit@k：expected_mart_ids 在 hits top-k 中命中幾筆."""
    found_ids = {str(h["_id"]) for h in hits}
    return sum(1 for mid in expected_mart_ids if str(mid) in found_ids)


def _format_hit_row(rank: int, knn_hit: dict | None, bm25_hit: dict | None) -> str:
    """格式化並排表一列."""
    def _fmt(hit: dict | None) -> str:
        if hit is None:
            return "—"
        mart_id = hit["_id"]
        name = (hit.get("_source") or {}).get("martName", "")[:20]
        score = hit.get("_score", 0)
        return f"{mart_id} {name} ({score:.3f})"

    return f"| {rank:4d} | {_fmt(knn_hit):<45} | {_fmt(bm25_hit):<45} |"


# ---------- 主流程 ----------


def main() -> None:
    from opensearchpy import OpenSearch  # noqa: PLC0415

    date_str = sys.argv[1] if len(sys.argv) > 1 else DATE_PLACEHOLDER
    out_path = OUT_DIR / f"search_eval_{date_str}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Gate：status check（load_golden_set 內部 exit 1）
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

    # 分類污染示範（固定加跑）
    lines.append("\n## 分類污染示範（category filter vs 純向量）\n")
    lines.append(
        "查詢：「保健食品」，加 categoryLevel1Name=「保健食品」filter vs 純向量搜尋\n"
    )
    demo_query = "保健食品"
    demo_vector = embed_query(demo_query)

    # 加 filter 搜尋（可能漏掉 category=品牌名 的商品）
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

    # 純向量搜尋（無 filter）
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
