"""
P1-3 load script: batch-write product data into the OpenSearch products_v1 index.

Input   : products/OpenSearch_Full_20260612_030007.json
          (36MB single-line JSON; supports both plain-array and search-response hits formats)
Output  : OpenSearch http://localhost:9200 > index "products_v1"
          summary printed to stdout (total / filtered / error counts)

Strategy: algorithm-first, never fall back to LLM.
          _id=str(martId) + index action → idempotent on re-run (overwrites, no duplicates).
          refresh_interval=-1 during load, restored to 1s on completion.
Usage   : uv run python scripts/etl/load_products_os.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------- connection constants ----------

OS_HOST = "http://localhost:9200"
INDEX_NAME = os.environ.get("OPENSEARCH_INDEX", "products_v1")  # target index can be overridden
# knn_vector dimension (Cohere Embed v4 = 1536; can be overridden via EMBED_DIM)
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1536"))
SOURCE_FILE = Path("products/OpenSearch_Full_20260612_030007.json")
BULK_CHUNK_SIZE = 500

# ---------- index settings constants (used at creation; knn can't be changed live, so confirm here before review) ----------

INDEX_SETTINGS: dict = {
    "index.knn": True,
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "refresh_interval": "-1",   # disable auto-refresh during load, restore on completion
}

INDEX_MAPPING: dict = {
    "properties": {
        "martId": {"type": "keyword"},
        "martName": {"type": "text", "analyzer": "smartcn"},
        "feature": {"type": "text", "analyzer": "smartcn"},
        "keyword": {"type": "text", "analyzer": "smartcn"},
        "categoryLevel1Name": {"type": "keyword"},
        "categoryLevel2Name": {"type": "keyword"},
        "categoryLevel3Name": {"type": "keyword"},
        "brand": {"type": "keyword"},
        "price": {"type": "float"},
        "isSearchable": {"type": "integer"},
        "embedding": {
            "type": "knn_vector",
            "dimension": EMBED_DIM,
            "method": {
                "engine": "faiss",
                "name": "hnsw",
                "space_type": "innerproduct",
            },
        },
    }
}

# ---------- pure functions (importable by tests) ----------


def detect_format(raw: object) -> str:
    """Detect the source JSON's top-level structure; return 'plain_array' or 'search_hits'.

    Args:
        raw: the top-level object after json.load().

    Returns:
        'plain_array'  — top level is a list whose first element is a product dict (with martId).
        'search_hits'  — top level is a list whose first element has a '_source' key (search-response hits).

    Raises:
        ValueError: unknown structure; fail fast, no guessing, no LLM fallback.
    """
    if not isinstance(raw, list) or len(raw) == 0:
        raise ValueError(
            f"未知 JSON 結構：頂層不是非空 list，實際 type={type(raw).__name__}"
        )
    first = raw[0]
    if not isinstance(first, dict):
        raise ValueError(
            f"未知 JSON 結構：第一個元素不是 dict，實際 type={type(first).__name__}"
        )
    if "_source" in first:
        return "search_hits"
    if "martId" in first:
        return "plain_array"
    raise ValueError(
        f"未知 JSON 結構：第一個元素既無 '_source' 也無 'martId'。"
        f"實際 keys={list(first.keys())[:10]}"
    )


def extract_sources(raw: list) -> list[dict]:
    """Uniformly extract the source-dict list from either top-level list format.

    Args:
        raw: the top-level list after json.load() (must pass detect_format first).

    Returns:
        list of source dicts (product fields are directly at the dict level).

    Raises:
        ValueError: unrecognizable format (delegated to detect_format).
    """
    fmt = detect_format(raw)
    if fmt == "plain_array":
        return list(raw)
    # fmt == "search_hits"
    return [item["_source"] for item in raw if "_source" in item]


# ---------- main flow (all IO inside the main guard; import has zero side effects) ----------


def main() -> None:
    from opensearchpy import OpenSearch, helpers  # noqa: PLC0415

    client = OpenSearch(
        hosts=[OS_HOST],
        timeout=60,  # large bulk writes; the default 10s is too short
        max_retries=3,
        retry_on_timeout=True,
    )

    # 1. create the index (skip if it already exists)
    if not client.indices.exists(index=INDEX_NAME):
        print(f"建立索引 {INDEX_NAME} …")
        client.indices.create(
            index=INDEX_NAME,
            body={"settings": INDEX_SETTINGS, "mappings": INDEX_MAPPING},
        )
        print(f"  索引 {INDEX_NAME} 建立成功。")
    else:
        print(f"索引 {INDEX_NAME} 已存在，跳過建立。")

    # 2. read the source JSON
    print(f"讀取 {SOURCE_FILE} …")
    with SOURCE_FILE.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    sources = extract_sources(raw)
    total_raw = len(sources)
    print(f"  來源筆數（全格式展開後）：{total_raw}")

    # 3. filter out isSearchable != 1
    filtered = [s for s in sources if s.get("isSearchable") == 1]
    excluded = total_raw - len(filtered)
    print(f"  過濾 isSearchable=0：排除 {excluded} 筆，保留 {len(filtered)} 筆")

    # 4. bulk index (action=index + _id=str(martId) → idempotent overwrite)
    skipped_no_id = 0

    def _actions():
        nonlocal skipped_no_id
        for doc in filtered:
            mart_id = doc.get("martId")
            if not mart_id:
                skipped_no_id += 1
                continue
            yield {
                "_op_type": "index",
                "_index": INDEX_NAME,
                "_id": str(mart_id),
                "_source": doc,
            }

    print(f"開始 bulk index（chunk={BULK_CHUNK_SIZE}）…")
    try:
        success_count, errors = helpers.bulk(
            client,
            _actions(),
            chunk_size=BULK_CHUNK_SIZE,
            raise_on_error=False,
            stats_only=False,
        )
    finally:
        # Fix 2: try/finally guard, ensuring refresh_interval is restored in all cases
        print("復原 refresh_interval → 1s …")
        client.indices.put_settings(
            index=INDEX_NAME,
            body={"index": {"refresh_interval": "1s"}},
        )
        client.indices.refresh(index=INDEX_NAME)

    error_count = len(errors) if isinstance(errors, list) else errors

    # 6. summary
    count_resp = client.count(index=INDEX_NAME)
    doc_count = count_resp.get("count", "?")
    print("\n===== Summary =====")
    print(f"  來源總筆數  : {total_raw}")
    print(f"  過濾排除    : {excluded} 筆")
    print(f"  跳過無ID    : {skipped_no_id} 筆")
    print(f"  Bulk 成功   : {success_count} 筆")
    print(f"  Bulk 錯誤   : {error_count} 筆")
    print(f"  索引文件數  : {doc_count}")
    print("===================")

    if error_count > 0:
        print(f"[ERROR] bulk 發生 {error_count} 筆錯誤，請檢查上方 log。", file=sys.stderr)
        sys.exit(1)

    print("完成。")


if __name__ == "__main__":
    main()
