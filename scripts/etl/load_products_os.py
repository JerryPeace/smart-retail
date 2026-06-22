"""
P1-3 載入腳本：將商品資料批次寫入 OpenSearch products_v1 索引.

輸入  : products/OpenSearch_Full_20260612_030007.json
        （36MB 單行 JSON；支援 plain array 與 search-response hits 兩種格式）
輸出  : OpenSearch http://localhost:9200 > index "products_v1"
        summary 印至 stdout（總筆數/過濾筆數/錯誤筆數）

策略  : 演算法優先、絕不 fallback LLM。
        _id=str(martId) + index action → 重跑冪等（覆寫不翻倍）。
        載入期 refresh_interval=-1，完成後復原 1s。
用法  : uv run python scripts/etl/load_products_os.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------- 連線常數 ----------

OS_HOST = "http://localhost:9200"
INDEX_NAME = os.environ.get("OPENSEARCH_INDEX", "products_v1")  # 可覆寫目標索引
# knn_vector 維度（Cohere Embed v4 = 1536；可由 EMBED_DIM 覆寫）
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1536"))
SOURCE_FILE = Path("products/OpenSearch_Full_20260612_030007.json")
BULK_CHUNK_SIZE = 500

# ---------- 索引設定常數（建立時用，knn 不可熱改，需 review 前先在此確認）----------

INDEX_SETTINGS: dict = {
    "index.knn": True,
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "refresh_interval": "-1",   # 載入期關掉自動 refresh，完成後復原
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

# ---------- 純函式（可被測試 import）----------


def detect_format(raw: object) -> str:
    """偵測來源 JSON 頂層結構，回傳 'plain_array' 或 'search_hits'.

    Args:
        raw: json.load() 後的頂層物件。

    Returns:
        'plain_array'  — 頂層是 list，第一個元素是商品 dict（含 martId）。
        'search_hits'  — 頂層是 list，第一個元素含 '_source' 鍵（search-response hits）。

    Raises:
        ValueError: 未知結構，fail fast，不猜測、不 fallback LLM。
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
    """將兩種格式的頂層 list 統一抽出 source dict 串.

    Args:
        raw: json.load() 後的頂層 list（必須先通過 detect_format）。

    Returns:
        list of source dict（商品欄位直接在 dict 層級）。

    Raises:
        ValueError: 格式無法識別（代理給 detect_format）。
    """
    fmt = detect_format(raw)
    if fmt == "plain_array":
        return list(raw)
    # fmt == "search_hits"
    return [item["_source"] for item in raw if "_source" in item]


# ---------- 主流程（IO 全部在 main guard 內，import 零副作用）----------


def main() -> None:
    from opensearchpy import OpenSearch, helpers  # noqa: PLC0415

    client = OpenSearch(
        hosts=[OS_HOST],
        timeout=60,  # 大批 bulk 寫入,預設 10s 偏短
        max_retries=3,
        retry_on_timeout=True,
    )

    # 1. 建立索引（已存在則跳過）
    if not client.indices.exists(index=INDEX_NAME):
        print(f"建立索引 {INDEX_NAME} …")
        client.indices.create(
            index=INDEX_NAME,
            body={"settings": INDEX_SETTINGS, "mappings": INDEX_MAPPING},
        )
        print(f"  索引 {INDEX_NAME} 建立成功。")
    else:
        print(f"索引 {INDEX_NAME} 已存在，跳過建立。")

    # 2. 讀取來源 JSON
    print(f"讀取 {SOURCE_FILE} …")
    with SOURCE_FILE.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    sources = extract_sources(raw)
    total_raw = len(sources)
    print(f"  來源筆數（全格式展開後）：{total_raw}")

    # 3. 過濾 isSearchable != 1
    filtered = [s for s in sources if s.get("isSearchable") == 1]
    excluded = total_raw - len(filtered)
    print(f"  過濾 isSearchable=0：排除 {excluded} 筆，保留 {len(filtered)} 筆")

    # 4. bulk index（action=index + _id=str(martId) → 冪等覆寫）
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
        # Fix 2: try/finally 保護，確保任何情況下都復原 refresh_interval
        print("復原 refresh_interval → 1s …")
        client.indices.put_settings(
            index=INDEX_NAME,
            body={"index": {"refresh_interval": "1s"}},
        )
        client.indices.refresh(index=INDEX_NAME)

    error_count = len(errors) if isinstance(errors, list) else errors

    # 6. Summary
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
