"""
LLM-judge 搜尋相關性評估腳本：以 LLM-as-judge 取代精確 expected_mart_id 命中量尺.

背景
----
原「hit@k 精確命中」量尺對「多 SKU 變體 + 通用語意 query」不公正——
向量搜尋找到語意相關但 expected_mart_ids 未列舉的商品，仍被判為失敗。
本腳本改以 LLM-judge 評「相關性」，每個（query, 商品）二元判定：
  relevant: true/false + reason（≤15字）。
expected_mart_ids 仍保留在輸出中供參考，但不作為唯一標準答案。

輸入
----
- scripts/etl/golden_set_product_search.yaml（meta.status 必須為 approved）
- OpenSearch http://localhost:9200 > index "products_v1"（已嵌入 26,014 筆）
- AWS Bedrock（profile=lab, region=ap-northeast-1）
  judge 模型：jp.anthropic.claude-haiku-4-5-20251001-v1:0
  embed 模型：amazon.titan-embed-text-v2:0（重用 verify_search_os.embed_query）

輸出
----
out/search_eval_judge_{YYYYMMDD}.md

成功標準（來自 meta.success_threshold_N，預設 3）
------------------------------------------------
non_overlap 8 條中，「向量勝」（vec_rel@10 > bm25_rel@10）的 query 數 ≥ N。
相同時算平手，不計入向量勝。

judge 呼叫估算
--------------
15 query × 平均 ~17 unique 商品 ≈ 255 次呼叫（dict 快取，同一對只 judge 一次）
Haiku：input ~$0.0008/K token，每次呼叫約 ~220 token
255 × 220 token ≈ 56,100 token ≈ $0.045 — 成本極低

安全
----
本腳本打真 Bedrock（judge + embed），執行前需取得使用者同意（safety.md §1）。

用法
----
uv run python scripts/etl/judge_search_relevance.py [YYYYMMDD]
YYYYMMDD 省略時使用 DATE_PLACEHOLDER（不依賴 datetime.now()，對齊 verify 慣例）
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

# ---------- 常數 ----------

OS_HOST = "http://localhost:9200"
GOLDEN_SET_PATH = Path("scripts/etl/golden_set_product_search.yaml")
OUT_DIR = Path("out")
DATE_PLACEHOLDER = "YYYYMMDD"

BEDROCK_PROFILE = "lab"
BEDROCK_REGION = "ap-northeast-1"
# 可用 JUDGE_MODEL_ID 環境變數覆寫（如 jp.anthropic.claude-opus-4-8 做高信度重評）
JUDGE_MODEL_ID = os.environ.get(
    "JUDGE_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
)

SEARCH_K = 10
FEATURE_MAX_CHARS = 200   # judge prompt 中 feature 截斷長度
JUDGE_WORKERS = 8          # ThreadPoolExecutor 並發 judge 數
RETRY_MAX = 6              # 指數退避最大重試次數

# ---------- importlib 載入 verify_search_os（重用不重寫）----------
# 以 __file__ 絕對路徑定位，避免 working directory 影響


def _load_verify_mod():
    """以 importlib 安全載入 verify_search_os.py（不觸發 __main__ guard）.

    使用 Path(__file__).parent 計算絕對路徑，讓腳本從任何工作目錄執行都有效。

    Returns:
        verify_search_os module object（含 load_golden_set, embed_query,
        knn_search, bm25_search, INDEX_NAME 等屬性）。
    """
    path = Path(__file__).parent / "verify_search_os.py"
    spec = importlib.util.spec_from_file_location("verify_search_os", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------- HTML 清理 ----------


def _strip_html(text: str) -> str:
    """移除 HTML 標籤並 unescape 實體，回傳純文字."""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ---------- LLM Judge ----------


def _build_judge_prompt(query: str, mart_name: str, feature_snippet: str) -> str:
    """組建 judge prompt，要求只回傳 JSON {"relevant": bool, "reason": str}.

    設計原則（ai-prompting.md）：
    - 給「為什麼」讓模型在邊界情境做對判斷（語意相關即可）
    - 給反面示例防止誤判
    - 要求 JSON-only 避免包裝文字干擾解析
    - reason 限 15 字，節省 output token

    Args:
        query: 使用者搜尋 query。
        mart_name: 商品名稱（martName）。
        feature_snippet: 商品 feature 前 200 字（已 strip HTML）。

    Returns:
        完整 prompt 字串。
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
    """對單一（query, 商品）呼叫 judge LLM，含指數退避重試.

    Args:
        session: boto3.Session（per-thread 建立，避免跨 thread 共用）。
        query: 搜尋 query。
        mart_name: 商品名稱。
        feature: 商品 feature 原文（此函式內部做 strip + truncate）。

    Returns:
        {"relevant": bool, "reason": str}
        解析失敗時回傳 {"relevant": False, "reason": "解析失敗"}。

    Raises:
        ExpiredTokenException: 憑證過期，上層統一處理並印 refresh 指引。
        RuntimeError: 超過最大重試次數後仍失敗。
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
            # 解析 JSON（處理 LLM 可能包 markdown code fence 的情況）
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

            # 憑證過期 → 直接 raise，讓上層統一處理
            if "ExpiredToken" in exc_name or "ExpiredTokenException" in exc_str:
                raise

            # Throttling / 5xx → 指數退避
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

            # 其他 exception → 不重試，回傳失敗結果
            return {"relevant": False, "reason": f"呼叫異常:{exc_name}"}

    raise RuntimeError(f"超過最大重試次數 {RETRY_MAX}") from last_exc


# ---------- 並發 judge ----------

JudgeKey = tuple[str, str]  # (query_id, martId)
JudgeCache = dict[JudgeKey, dict[str, Any]]


def _judge_batch(
    items: list[tuple[JudgeKey, str, str, str]],
    cache: JudgeCache,
) -> None:
    """並發 judge 一批（query_id, martId, query_text, mart_name, feature）.

    同一 JudgeKey 只 judge 一次（快取去重）。
    ExpiredToken 時印 refresh 指引、salvage 已完成結果後 sys.exit(1)。

    Args:
        items: list of (key, query_text, mart_name, feature)。
        cache: 已完成快取（in-place 更新，key = JudgeKey）。

    Side-effects:
        更新 cache in-place。
        ExpiredToken 時 sys.exit(1)。
    """
    import boto3  # noqa: PLC0415

    # 過濾已快取的項目（不重複 judge）
    pending = [item for item in items if item[0] not in cache]
    if not pending:
        return

    expired_flag = {"hit": False}

    def _worker(item: tuple[JudgeKey, str, str, str]) -> tuple[JudgeKey, dict]:
        key, query_text, mart_name, feature = item
        # per-thread session（避免 client 跨 thread 共用）
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


# ---------- 主流程 ----------


def main() -> None:
    """LLM-judge 搜尋相關性評估主流程."""
    from opensearchpy import OpenSearch  # noqa: PLC0415

    # ── 初始化 ──
    date_str = sys.argv[1] if len(sys.argv) > 1 else DATE_PLACEHOLDER
    out_path = OUT_DIR / f"search_eval_judge_{date_str}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 動態載入 verify_search_os 重用其函式 ──
    verify_mod = _load_verify_mod()
    load_golden_set = verify_mod.load_golden_set
    embed_query = verify_mod.embed_query
    knn_search = verify_mod.knn_search
    bm25_search = verify_mod.bm25_search
    index_name = verify_mod.INDEX_NAME

    # ── Gate：status check（load_golden_set 內部 exit 1）──
    golden = load_golden_set(GOLDEN_SET_PATH)
    queries = golden.get("queries", [])
    meta = golden.get("meta", {})
    success_n = int(meta.get("success_threshold_N", 3))
    print(f"Golden set loaded：{len(queries)} 條查詢  成功門檻 N={success_n}")

    # ── OpenSearch client（加 timeout/retry）──
    os_client = OpenSearch(
        hosts=[OS_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True,
    )

    # ── Phase 1：嵌入查詢 & 取 vector/BM25 top-10 ──
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

    # ── Phase 2：收集所有需 judge 的（query_id, martId）聯集去重 ──
    judge_cache: JudgeCache = {}
    judge_items: list[tuple[JudgeKey, str, str, str]] = []
    scheduled: set[JudgeKey] = set()

    for qr in query_results:
        qid = qr["qid"]
        query_text = qr["query_text"]
        # 聯集（vec + bm25），by martId 去重
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

    # ── Phase 3：計算每 query 指標 ──
    print("[Phase 3] 計算指標 & 輸出報告…")

    per_query_metrics: list[dict] = []
    lines: list[str] = []

    # 報告標頭
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

    # 聚合計數器
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

        # vec_rel@10：vector top-10 中 relevant 數量
        vec_rel_count = sum(
            1 for h in vec_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
        )
        # bm25_rel@10：BM25 top-10 中 relevant 數量
        bm25_rel_count = sum(
            1 for h in bm25_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
        )
        # vec_only_rel：在 vec top-10 且 relevant、但不在 bm25 top-10
        vec_only_rel_ids = {
            str(h["_id"])
            for h in vec_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
            and str(h["_id"]) not in bm25_ids
        }
        # bm25_only_rel：在 bm25 top-10 且 relevant、但不在 vec top-10
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

        # 聚合
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

        # ── 每 query 報告區塊 ──
        lines.append(f"\n## {qid}「{query_text}」 ({category})\n")
        lines.append(f"> rationale: {qr['rationale']}  \n")
        lines.append(f"> expected_mart_ids（僅供參考）: {qr['expected_ids']}\n")
        lines.append(
            f"> 指標：**vec_rel@10={vec_rel_count}** | **bm25_rel@10={bm25_rel_count}** | "
            f"vec_only_rel={vec_only_rel_count} | bm25_only_rel={bm25_only_rel_count}\n"
        )

        # 向量 top-10 表
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

        # BM25 top-10 表
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

        # non_overlap 判定
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

    # ── 每 query 彙總表 ──
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

    # 成功標準判定
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
