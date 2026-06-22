"""
P1-4 向量化腳本：對 OpenSearch 中缺少 embedding 的商品呼叫 Bedrock Cohere Embed v4 嵌入並寫回.

輸入  : OpenSearch http://localhost:9200 > index（OPENSEARCH_INDEX 覆寫，需先完成載入）
        AWS Bedrock (profile=lab, region=ap-northeast-1) cohere.embed-v4:0
輸出  : 每筆文件 embedding 欄位（output_dimension=1536，L2 正規化 → innerproduct=cosine）

策略  :
  - 只補缺：query must_not exists "embedding" 取待嵌入 doc，重跑自動續跑
  - 不維護進度檔：「無 embedding 欄」即進度狀態
  - per-thread boto3 session（session-per-thread 最穩，boto3 client 跨執行緒有風險）
  - exponential backoff：429/5xx/Throttling max 8 次，ValidationException fail fast
  - ThreadPoolExecutor 預設 8 workers
  - ExpiredTokenException 時印 refresh-creds 指引後結束，重跑即續跑

安全  : 執行本腳本前必須告知使用者預估成本（26k×Cohere v4 ≈ <$1 一次性）並取得同意。
        詳見 .claude/rules/safety.md §1（Bedrock 花費）。
用法  : OPENSEARCH_INDEX=products_v5_cohere uv run python scripts/etl/embed_products_os.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# ---------- 連線 / 執行常數 ----------

OS_HOST = "http://localhost:9200"
INDEX_NAME = os.environ.get("OPENSEARCH_INDEX", "products_v1")  # 可覆寫嵌入目標索引
# EMBED_NO_FEATURE=1 → 嵌入文字排除 feature 欄（清污染實驗：feature 行銷套話污染向量）
EMBED_NO_FEATURE = os.environ.get("EMBED_NO_FEATURE") == "1"
BEDROCK_PROFILE = "lab"
BEDROCK_REGION = "ap-northeast-1"
BEDROCK_MODEL_ID = "cohere.embed-v4:0"   # Cohere Embed v4（中文語意檢索優於 Titan v2）
EMBED_DIMENSIONS = 1536                    # Cohere v4 原生最高品質維度
MAX_WORKERS = 8          # ThreadPoolExecutor 並發數（依 Bedrock RPM quota 調整）
BULK_BATCH_SIZE = 300    # 每批 update bulk 大小（200~500 皆可）
SCROLL_PAGE_SIZE = 500   # scroll 一次取幾筆
MAX_EMBED_CHARS = 50_000  # Titan v2 上限保守界
RETRY_BASE_SECS = 1.0    # backoff 起始秒數
RETRY_FACTOR = 2         # 指數倍率
RETRY_MAX = 8            # 最大重試次數

# ---------- 純函式（可被測試 import）----------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """移除 HTML tag，以空白取代（POC 不引入 bs4）."""
    return _HTML_TAG_RE.sub(" ", text)


def _l2_normalize(vec: list[float]) -> list[float]:
    """L2 正規化成單位向量。

    Titan v2 內建 normalize=True 已是單位向量；Cohere 的 float embedding 非單位長，
    需在此手動正規化，才能讓索引的 innerproduct 度量等價 cosine（與 query 端一致）。
    """
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def build_embed_text(source: dict) -> str:
    """從商品 source dict 組裝嵌入用文字字串.

    組裝順序：martName → feature → keyword → categoryLevel1Name
              → categoryLevel2Name → categoryLevel3Name
    清洗規則：
      - feature strip HTML（regex <[^>]+> → 空白）
      - 所有欄位以 `doc.get(field) or ""` 取值，防 None 被串成字面 "None"
      - 多空白壓縮成單一空白，首尾 strip
      - 全體 truncate 至 MAX_EMBED_CHARS 字元（Titan v2 上限 8192 token 保守界）

    Args:
        source: 來自 OpenSearch _source 的商品欄位 dict。

    Returns:
        清洗後的嵌入文字（不含字面 "None"）。
    """
    mart_name = source.get("martName") or ""
    # 清污染實驗：EMBED_NO_FEATURE=1 時排除 feature（其行銷套話污染向量空間）
    feature = "" if EMBED_NO_FEATURE else strip_html(source.get("feature") or "")
    keyword = source.get("keyword") or ""
    cat1 = source.get("categoryLevel1Name") or ""
    cat2 = source.get("categoryLevel2Name") or ""
    cat3 = source.get("categoryLevel3Name") or ""

    combined = "\n".join(
        part for part in [mart_name, feature, keyword, cat1, cat2, cat3] if part
    )
    # 多空白壓縮（換行也算 \s）
    combined = _MULTI_SPACE_RE.sub(" ", combined).strip()
    return combined[:MAX_EMBED_CHARS]


# ---------- Bedrock 嵌入（thread-local session，main guard 內呼叫）----------

_thread_local = threading.local()


def _get_bedrock_client() -> Any:
    """取得 per-thread boto3 bedrock-runtime client（lazy init）."""
    import boto3  # noqa: PLC0415

    if not hasattr(_thread_local, "client"):
        session = boto3.Session(
            profile_name=BEDROCK_PROFILE,
            region_name=BEDROCK_REGION,
        )
        _thread_local.client = session.client("bedrock-runtime")
    return _thread_local.client


def _embed_with_retry(text: str) -> list[float]:
    """呼叫 Bedrock Cohere Embed v4 取得 embedding，帶 exponential backoff 重試.

    重試條件：ThrottlingException / 429 / 5xx
    fail fast：ValidationException 等非暫時性錯誤直接拋出
    ExpiredTokenException：呼叫方捕捉，印指引後結束

    Args:
        text: 嵌入文字（已清洗）。

    Returns:
        長度 EMBED_DIMENSIONS（1536）的 L2 正規化 float list。
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    # Cohere Embed v4：texts / input_type=search_document（文件端）/ output_dimension。
    # 回 embeddings.float[0]，非單位長 → L2 正規化讓索引 innerproduct 等價 cosine（與 query 端一致）。
    body = json.dumps(
        {
            "texts": [text],
            "input_type": "search_document",
            "embedding_types": ["float"],
            "output_dimension": EMBED_DIMENSIONS,
            "truncate": "RIGHT",  # Bedrock 截尾值（非 Cohere 原生的 END）；超長 feature 截尾不中斷整批
        }
    )
    delay = RETRY_BASE_SECS
    for attempt in range(RETRY_MAX + 1):
        try:
            client = _get_bedrock_client()
            response = client.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(response["body"].read())
            emb = result["embeddings"]
            vec = emb["float"][0] if isinstance(emb, dict) else emb[0]
            return _l2_normalize(vec)
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            http_status = exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode", 0
            )
            # 憑證過期：直接往上拋（main 捕捉後印指引）
            if error_code in ("ExpiredTokenException", "ExpiredToken"):
                raise
            # 非暫時性錯誤 fail fast
            if error_code in ("ValidationException", "AccessDeniedException"):
                raise
            # 暫時性錯誤 → backoff 重試
            is_throttle = error_code in (
                "ThrottlingException",
                "TooManyRequestsException",
                "ServiceUnavailableException",
            )
            is_server_err = http_status >= 500
            if (is_throttle or is_server_err) and attempt < RETRY_MAX:
                jitter = delay * 0.1 * (hash(text) % 10 / 10)  # 簡易 jitter
                sleep_time = delay + jitter
                print(
                    f"  [retry {attempt + 1}/{RETRY_MAX}] {error_code}，"
                    f"等待 {sleep_time:.1f}s …"
                )
                time.sleep(sleep_time)
                delay *= RETRY_FACTOR
                continue
            raise


def main() -> None:  # noqa: C901
    import boto3  # noqa: F401, PLC0415
    from botocore.exceptions import ClientError  # noqa: PLC0415
    from opensearchpy import OpenSearch, helpers  # noqa: PLC0415

    client = OpenSearch(
        hosts=[OS_HOST],
        timeout=60,  # bulk 寫回大批向量,預設 10s 會 ConnectionTimeout
        max_retries=3,
        retry_on_timeout=True,
    )

    total_embedded = 0
    round_num = 0

    while True:
        round_num += 1
        # 1. 查詢缺 embedding 的文件（scroll）
        query = {
            "query": {"bool": {"must_not": [{"exists": {"field": "embedding"}}]}},
            "_source": [
                "martId",
                "martName",
                "feature",
                "keyword",
                "categoryLevel1Name",
                "categoryLevel2Name",
                "categoryLevel3Name",
            ],
            "size": SCROLL_PAGE_SIZE,
        }
        resp = client.search(index=INDEX_NAME, body=query, scroll="5m")
        scroll_id = resp.get("_scroll_id")
        hits = resp["hits"]["hits"]

        if not hits:
            break

        pending_count_resp = client.count(
            index=INDEX_NAME,
            body={
                "query": {
                    "bool": {"must_not": [{"exists": {"field": "embedding"}}]}
                }
            },
        )
        pending_total = pending_count_resp.get("count", "?")
        print(f"\n[Round {round_num}] 本輪起始缺 embedding：{pending_total} 筆")

        # 2. 逐頁取 hits → 並發嵌入
        all_hits_this_round: list[dict] = []
        all_hits_this_round.extend(hits)

        while hits:
            resp = client.scroll(scroll_id=scroll_id, scroll="5m")
            hits = resp["hits"]["hits"]
            all_hits_this_round.extend(hits)

        if scroll_id:
            try:
                client.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass

        print(f"  本輪取得待嵌入 doc 數：{len(all_hits_this_round)}")

        # 3. ThreadPoolExecutor 並發嵌入 + 增量 flush（每批落地，憑證過期不丟資料）

        def _embed_doc(hit: dict) -> tuple[str, list[float]]:
            source = hit["_source"]
            mart_id = str(hit["_id"])
            text = build_embed_text(source)
            vector = _embed_with_retry(text)
            return mart_id, vector

        def _flush(buffer: dict[str, list[float]]) -> int:
            """將 buffer 的 embedding 以 bulk update 寫回 OpenSearch，回傳成功筆數."""
            if not buffer:
                return 0

            def _actions():
                for mid, vec in buffer.items():
                    yield {
                        "_op_type": "update",
                        "_index": INDEX_NAME,
                        "_id": mid,
                        "doc": {"embedding": vec},
                    }

            success, errors = helpers.bulk(
                client,
                _actions(),
                chunk_size=BULK_BATCH_SIZE,
                raise_on_error=False,
                stats_only=False,
            )
            error_count = len(errors) if isinstance(errors, list) else errors
            print(f"  [flush] bulk update 成功 {success}，錯誤 {error_count}")
            return success

        flush_buffer: dict[str, list[float]] = {}
        done_count = 0
        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(_embed_doc, h): h
                    for h in all_hits_this_round
                }
                for future in as_completed(futures):
                    mart_id, vector = future.result()
                    flush_buffer[mart_id] = vector
                    done_count += 1
                    if done_count % 200 == 0:
                        print(f"  … 已完成 {done_count}/{len(all_hits_this_round)}")
                    # 達到批次大小就寫回，持續落地進度
                    if len(flush_buffer) >= BULK_BATCH_SIZE:
                        total_embedded += _flush(flush_buffer)
                        flush_buffer = {}
            # 正常結束後 flush 剩餘
            total_embedded += _flush(flush_buffer)
            flush_buffer = {}
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code in ("ExpiredTokenException", "ExpiredToken"):
                # 先 flush 已算好的 embedding，確保不因憑證過期而丟失
                if flush_buffer:
                    print("  [憑證過期前 flush 剩餘 buffer …]")
                    total_embedded += _flush(flush_buffer)
                    flush_buffer = {}
                print(
                    "\n[ERROR] AWS lab 憑證已過期。請執行：\n"
                    "  ./scripts/refresh-lab-creds.sh\n"
                    "然後重跑同一指令，腳本會自動從缺漏處續跑。",
                    file=sys.stderr,
                )
                sys.exit(1)
            raise

        # 4. round 結尾印本輪累計
        print(f"  本輪嵌入完成，本輪小計：{done_count} 筆（累計 {total_embedded}）")

    # 5. 結尾 summary
    remaining_resp = client.count(
        index=INDEX_NAME,
        body={
            "query": {"bool": {"must_not": [{"exists": {"field": "embedding"}}]}}
        },
    )
    remaining = remaining_resp.get("count", "?")

    print("\n===== Summary =====")
    print(f"  本次執行嵌入筆數 : {total_embedded}")
    print(f"  剩餘缺 embedding : {remaining}（0 = 全部完成）")
    print("===================")

    if remaining != 0:
        print(
            "\n提示：尚有文件缺 embedding，重跑同一指令即可續跑。\n"
            "如遇憑證過期請先執行 ./scripts/refresh-lab-creds.sh"
        )


if __name__ == "__main__":
    main()
