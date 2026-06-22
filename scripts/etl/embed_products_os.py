"""
P1-4 embedding script: for products in OpenSearch that lack an embedding, call Bedrock Cohere Embed v4 to embed them and write the result back.

Input   : OpenSearch http://localhost:9200 > index (override with OPENSEARCH_INDEX; loading must be done first)
          AWS Bedrock (profile=lab, region=ap-northeast-1) cohere.embed-v4:0
Output  : an embedding field on each document (output_dimension=1536, L2-normalized -> innerproduct=cosine)

Strategy:
  - Fill gaps only: query must_not exists "embedding" to fetch docs pending embedding; rerunning resumes automatically
  - No progress file maintained: "no embedding field" is the progress state itself
  - per-thread boto3 session (session-per-thread is most stable; a boto3 client shared across threads is risky)
  - exponential backoff: up to 8 retries on 429/5xx/Throttling; fail fast on ValidationException
  - ThreadPoolExecutor with 8 workers by default
  - On ExpiredTokenException, print the refresh-creds guidance and exit; rerunning resumes

Safety  : Before running this script you must tell the user the estimated cost (26k x Cohere v4 ~= <$1, one-off) and get consent.
          See .claude/rules/safety.md section 1 (Bedrock cost).
Usage   : OPENSEARCH_INDEX=products_v5_cohere uv run python scripts/etl/embed_products_os.py
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

# ---------- Connection / runtime constants ----------

OS_HOST = "http://localhost:9200"
INDEX_NAME = os.environ.get("OPENSEARCH_INDEX", "products_v1")  # the target index for embedding can be overridden
# EMBED_NO_FEATURE=1 -> exclude the feature field from the embed text (decontamination experiment: marketing boilerplate in feature pollutes the vector)
EMBED_NO_FEATURE = os.environ.get("EMBED_NO_FEATURE") == "1"
BEDROCK_PROFILE = "lab"
BEDROCK_REGION = "ap-northeast-1"
BEDROCK_MODEL_ID = "cohere.embed-v4:0"   # Cohere Embed v4 (better Chinese semantic retrieval than Titan v2)
EMBED_DIMENSIONS = 1536                    # Cohere v4 native highest-quality dimension
MAX_WORKERS = 8          # ThreadPoolExecutor concurrency (tune to your Bedrock RPM quota)
BULK_BATCH_SIZE = 300    # bulk update batch size (200~500 all work)
SCROLL_PAGE_SIZE = 500   # how many docs to fetch per scroll
MAX_EMBED_CHARS = 50_000  # conservative bound for the Titan v2 limit
RETRY_BASE_SECS = 1.0    # backoff starting seconds
RETRY_FACTOR = 2         # exponential factor
RETRY_MAX = 8            # max retry count

# ---------- Pure functions (importable by tests) ----------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """Remove HTML tags, replacing them with whitespace (the POC does not pull in bs4)."""
    return _HTML_TAG_RE.sub(" ", text)


def _l2_normalize(vec: list[float]) -> list[float]:
    """L2-normalize into a unit vector.

    Titan v2's built-in normalize=True already yields a unit vector; Cohere's float embedding
    is not unit length, so it must be normalized manually here so that the index's innerproduct
    metric is equivalent to cosine (consistent with the query side).
    """
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def build_embed_text(source: dict) -> str:
    """Assemble the text string for embedding from a product source dict.

    Assembly order: martName -> feature -> keyword -> categoryLevel1Name
              -> categoryLevel2Name -> categoryLevel3Name
    Cleaning rules:
      - strip HTML from feature (regex <[^>]+> -> whitespace)
      - read every field via `doc.get(field) or ""` to prevent None being concatenated as the literal "None"
      - collapse multiple spaces into a single space, strip leading/trailing whitespace
      - truncate the whole thing to MAX_EMBED_CHARS characters (conservative bound for Titan v2's 8192-token limit)

    Args:
        source: the product field dict from OpenSearch _source.

    Returns:
        the cleaned embed text (free of the literal "None").
    """
    mart_name = source.get("martName") or ""
    # Decontamination experiment: when EMBED_NO_FEATURE=1, exclude feature (its marketing boilerplate pollutes the vector space)
    feature = "" if EMBED_NO_FEATURE else strip_html(source.get("feature") or "")
    keyword = source.get("keyword") or ""
    cat1 = source.get("categoryLevel1Name") or ""
    cat2 = source.get("categoryLevel2Name") or ""
    cat3 = source.get("categoryLevel3Name") or ""

    combined = "\n".join(
        part for part in [mart_name, feature, keyword, cat1, cat2, cat3] if part
    )
    # collapse multiple spaces (newlines also count as \s)
    combined = _MULTI_SPACE_RE.sub(" ", combined).strip()
    return combined[:MAX_EMBED_CHARS]


# ---------- Bedrock embedding (thread-local session, called inside the main guard) ----------

_thread_local = threading.local()


def _get_bedrock_client() -> Any:
    """Get a per-thread boto3 bedrock-runtime client (lazy init)."""
    import boto3  # noqa: PLC0415

    if not hasattr(_thread_local, "client"):
        session = boto3.Session(
            profile_name=BEDROCK_PROFILE,
            region_name=BEDROCK_REGION,
        )
        _thread_local.client = session.client("bedrock-runtime")
    return _thread_local.client


def _embed_with_retry(text: str) -> list[float]:
    """Call Bedrock Cohere Embed v4 to get an embedding, with exponential backoff retries.

    Retry conditions: ThrottlingException / 429 / 5xx
    fail fast: non-transient errors such as ValidationException are raised directly
    ExpiredTokenException: caught by the caller, which prints guidance and exits

    Args:
        text: the embed text (already cleaned).

    Returns:
        an L2-normalized float list of length EMBED_DIMENSIONS (1536).
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    # Cohere Embed v4: texts / input_type=search_document (document side) / output_dimension.
    # Returns embeddings.float[0], not unit length -> L2-normalize so the index innerproduct is equivalent to cosine (consistent with the query side).
    body = json.dumps(
        {
            "texts": [text],
            "input_type": "search_document",
            "embedding_types": ["float"],
            "output_dimension": EMBED_DIMENSIONS,
            "truncate": "RIGHT",  # Bedrock's truncate value (not Cohere's native END); truncates over-long feature without aborting the whole batch
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
            # Expired credentials: re-raise directly (main catches it and prints guidance)
            if error_code in ("ExpiredTokenException", "ExpiredToken"):
                raise
            # fail fast on non-transient errors
            if error_code in ("ValidationException", "AccessDeniedException"):
                raise
            # Transient error -> backoff and retry
            is_throttle = error_code in (
                "ThrottlingException",
                "TooManyRequestsException",
                "ServiceUnavailableException",
            )
            is_server_err = http_status >= 500
            if (is_throttle or is_server_err) and attempt < RETRY_MAX:
                jitter = delay * 0.1 * (hash(text) % 10 / 10)  # simple jitter
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
        timeout=60,  # bulk writes back large batches of vectors; the default 10s would ConnectionTimeout
        max_retries=3,
        retry_on_timeout=True,
    )

    total_embedded = 0
    round_num = 0

    while True:
        round_num += 1
        # 1. Query documents missing an embedding (scroll)
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

        # 2. Fetch hits page by page -> embed concurrently
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

        # 3. ThreadPoolExecutor concurrent embedding + incremental flush (each batch is persisted, so expired credentials lose no data)

        def _embed_doc(hit: dict) -> tuple[str, list[float]]:
            source = hit["_source"]
            mart_id = str(hit["_id"])
            text = build_embed_text(source)
            vector = _embed_with_retry(text)
            return mart_id, vector

        def _flush(buffer: dict[str, list[float]]) -> int:
            """Write the buffer's embeddings back to OpenSearch via bulk update; return the success count."""
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
                    # Once the batch size is reached, write back to keep persisting progress
                    if len(flush_buffer) >= BULK_BATCH_SIZE:
                        total_embedded += _flush(flush_buffer)
                        flush_buffer = {}
            # After normal completion, flush the remainder
            total_embedded += _flush(flush_buffer)
            flush_buffer = {}
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code in ("ExpiredTokenException", "ExpiredToken"):
                # First flush the embeddings already computed, to make sure nothing is lost to expired credentials
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

        # 4. At the end of the round, print the round's running totals
        print(f"  本輪嵌入完成，本輪小計：{done_count} 筆（累計 {total_embedded}）")

    # 5. Final summary
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
