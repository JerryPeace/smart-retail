"""
Hybrid 搜尋準確度評估腳本：三路並排比較（hybrid / k-NN-only / BM25-only）.

背景
----
Phase 2 已實作 `/search` hybrid 端點（BM25 + k-NN，應用端 RRF 融合）。
本腳本評估 hybrid 是否優於/不劣於單一方法，重用 Phase 1 的 golden set 與
judge_search_relevance.py 的 LLM-judge 量尺。

輸入
----
- scripts/etl/golden_set_product_search.yaml（meta.status 必須為 approved）
- 運行中的 app（mock OFF）：GET http://localhost:8000/search?q=<query>&size=10
  取 hybrid top-10（app 必須先以 ANALYZER_MOCK_MODE=false 啟動）
- OpenSearch http://localhost:9200 > index "products_v1"（k-NN-only / BM25-only 直打）
- AWS Bedrock（profile=lab, region=ap-northeast-1）
  judge 模型：jp.anthropic.claude-opus-4-5（Opus 級，可用 JUDGE_MODEL_ID 環境變數覆寫）
  embed 模型：amazon.titan-embed-text-v2:0（重用 verify_search_os.embed_query）

輸出
----
out/search_eval_hybrid_{YYYYMMDD}.md（三欄並排 hybrid/knn/bm25 + Summary 兩項判定）

成功標準（design §10.3 / task 7.3）
---------------------------------------
(a) 全局：hybrid 總相關數 ≥ max(knn-only 總相關數, bm25-only 總相關數)
(b) 互補保留：
    - 向量強項 query（情境式，如 q11/q13）hybrid 相關數不歸零（hybrid_rel ≥ 1）
    - BM25 強項 query（q04 ThinkPad）hybrid 相關數不歸零（hybrid_rel ≥ 1）
兩項均達標 → 判定 ✅；任一未達 → 如實標 ❌，不調寬標準。

成本估算
--------
- 15 query × embed（Titan v2）：45 次嵌入（僅 k-NN 路徑）
- hybrid top-10 × 15 query：打 app /search 端點（不費 Bedrock，app 自行嵌入）
- k-NN + BM25 直打 OpenSearch：30 次查詢（無 Bedrock 費用）
- judge：15 query × 平均 ~17 unique 商品（三路聯集去重）≈ 255 次 Opus 呼叫
  Opus：input ~$15/M token，output ~$75/M token，每次呼叫約 ~300 token
  255 × 300 token ≈ 76,500 token ≈ 估計 < $2（Opus 級）
  全部費用量級 < $2，仍是真 Bedrock ——「執行前必須取得使用者同意（safety.md §1）」
- mget 補抓 hybrid-only doc source：最多 15 × SEARCH_K 次 mget（0 Bedrock 費用，OpenSearch only）

source_map artifact 修正說明
-----------------------------
原始實作 _build_source_map(knn_hits + bm25_hits) 只涵蓋兩路 top-SEARCH_K 的 doc。
hybrid 融合後若某 doc 排入 top-SEARCH_K 但在兩路中均排名更深（rank > SEARCH_K），
則該 doc 的 source 在 source_map 中缺失（空白商品名/feature）→ judge 看不到資訊 → 自動判不相關。
修正：Phase 1 取完三路 hits 後，對三路聯集中 source_map 缺失的 mart_id 發一次 mget 批次補抓，
確保每筆 judge item 都有完整商品資訊（_enrich_source_map_with_mget）。

safety 告知要求
---------------
本腳本打真 Bedrock（embed + judge），執行前「必須」向使用者告知預估成本並取得同意
（safety.md §1：真 Bedrock 呼叫前明示成本）。
執行前確認：
  1. app 已以 ANALYZER_MOCK_MODE=false 啟動（localhost:8000 健康）
  2. OpenSearch 在線（localhost:9200 可達）
  3. AWS lab 憑證有效（需要則執行 bash scripts/refresh-lab-creds.sh）
  4. 已取得使用者同意（成本告知 ≤ $2 量級）

gate
----
meta.status != approved 時 exit 1，不發任何外部呼叫（load_golden_set 內部強制）。

用法
----
uv run python scripts/etl/judge_hybrid_search.py [YYYYMMDD]
YYYYMMDD 省略時使用 DATE_PLACEHOLDER（不依賴 datetime.now()，對齊 verify 慣例）
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# ---------- 常數 ----------

APP_BASE_URL = "http://localhost:8000"
OS_HOST = "http://localhost:9200"
GOLDEN_SET_PATH = Path("scripts/etl/golden_set_product_search.yaml")
OUT_DIR = Path("out")
DATE_PLACEHOLDER = "YYYYMMDD"

BEDROCK_PROFILE = "lab"
BEDROCK_REGION = "ap-northeast-1"
# Opus 級 judge（高信度，避免跨輪漂移）；可用 JUDGE_MODEL_ID 環境變數覆寫
JUDGE_MODEL_ID = os.environ.get(
    "JUDGE_MODEL_ID", "jp.anthropic.claude-opus-4-5-20251001-v1:0"
)

SEARCH_K = 10

# 成功標準 (b)：互補保留的 query ID 清單
# 向量強項（情境式語意）：design §10.3 提及 q11/q13 為代表
VECTOR_STRONG_QUERIES = {"q11", "q13"}
# BM25 強項（詞面精準命中）：design §10.3 提及 q04 ThinkPad
BM25_STRONG_QUERIES = {"q04"}

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


def _load_judge_mod():
    """以 importlib 安全載入 judge_search_relevance.py（重用 judge 引擎）.

    比照 _load_verify_mod 模式，使用 Path(__file__).parent 計算絕對路徑。

    Returns:
        judge_search_relevance module object（含 _strip_html, _build_judge_prompt,
        _invoke_judge_single, _judge_batch, JudgeKey, JudgeCache,
        FEATURE_MAX_CHARS, JUDGE_WORKERS, RETRY_MAX 等屬性）。
    """
    path = Path(__file__).parent / "judge_search_relevance.py"
    spec = importlib.util.spec_from_file_location("judge_search_relevance", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------- Hybrid 搜尋：打運行中的 app /search 端點 ----------


def _fetch_hybrid_results(query_text: str, size: int = SEARCH_K) -> list[str]:
    """打運行中的 app GET /search，取 hybrid top-{size} mart_id 清單.

    Args:
        query_text: 搜尋查詢字串。
        size: 取 top-k 筆。

    Returns:
        list of mart_id (str)，依 RRF 分降序。
        連線失敗時拋 requests.exceptions.ConnectionError / httpx.ConnectError。
    """
    import requests  # noqa: PLC0415  (stdlib / pyproject.toml 依賴)

    url = f"{APP_BASE_URL}/search"
    resp = requests.get(url, params={"q": query_text, "size": size}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [item["mart_id"] for item in data.get("results", [])]


# ---------- 輔助：由 OpenSearch hit list 建立 martId→source 查找表 ----------


def _build_source_map(hits: list[dict]) -> dict[str, dict]:
    """由 OpenSearch hit list 建立 {martId: _source} 查找表.

    Args:
        hits: list of OpenSearch hit dict（含 _id、_source）。

    Returns:
        {str(hit["_id"]): hit["_source"]} dict。
    """
    return {str(h["_id"]): (h.get("_source") or {}) for h in hits}


def _enrich_source_map_with_mget(
    os_client: object,
    index_name: str,
    source_map: dict[str, dict],
    all_mart_ids: set[str],
) -> None:
    """mget 補抓 source_map 中缺失 doc 的 _source，in-place 填充。

    hybrid top-k 候選可能包含「只在某路深位（rank k+1~）」的 doc，
    這些 doc 不在 knn_hits/bm25_hits 的 top-k 中，source_map 因此空白。
    空白商品資訊會讓 judge 看不到 martName/feature → 自動判不相關（artifact）。

    本函式對三路聯集中 source_map 尚未覆蓋的 mart_id 發一次 mget 批次請求，
    補入 martName/feature/keyword/categoryLevelXName/brand/price 等 judge 需要的欄位。

    冪等設計：
    - 已有 source 的 mart_id 不重抓（即使部分欄位為空也跳過，避免重複費用）。
    - mget 回傳中 found=False 的 doc 不寫入（保留原空 dict，不覆蓋）。

    Args:
        os_client: opensearchpy.OpenSearch 實例（同步）。
        index_name: OpenSearch 索引名稱（e.g. "products_v1"）。
        source_map: {mart_id: _source dict}，in-place 填充缺失項目。
        all_mart_ids: 三路聯集的全部 mart_id set。
    """
    missing_ids = sorted(all_mart_ids - set(source_map))  # 排序保確定性
    if not missing_ids:
        return

    # mget 批次取 _source（只抓 judge 需要的欄位，減少 payload）
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


# ---------- 輔助：渲染 hit 清單為 Markdown 表格行 ----------


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
    """將 hit 清單渲染為 Markdown 表格，append 進 lines.

    Args:
        lines:       輸出行緩衝（in-place append）。
        hits:        hit 清單；有 score 時為 list[dict]（_id/_score/_source），
                     無 score 時（hybrid）為 list[str]（mart_id）。
        qid:         query id，供查 judge_cache。
        judge_cache: {(qid, mart_id): {"relevant": bool, "reason": str}}。
        source_map:  {mart_id: _source dict}，供查 martName。
        has_score:   True → 表格有 score 欄位（knn/bm25 路徑）；
                     False → 無 score 欄位（hybrid 路徑）。
        title:       表格段落標題（不含 ###）。
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


# ---------- 主流程 ----------


def main() -> None:
    """Hybrid 搜尋三路比較主流程（hybrid / k-NN-only / BM25-only）.

    執行前提：
    1. app 已以 ANALYZER_MOCK_MODE=false 啟動（localhost:8000）
    2. OpenSearch 在線（localhost:9200）
    3. AWS lab 憑證有效
    4. 已取得使用者同意（本腳本打真 Bedrock，成本 < $2 量級）
    """
    from opensearchpy import OpenSearch  # noqa: PLC0415

    # ── 初始化 ──
    date_str = sys.argv[1] if len(sys.argv) > 1 else DATE_PLACEHOLDER
    out_path = OUT_DIR / f"search_eval_hybrid_{date_str}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 動態載入模組（零網路，importlib only）──
    verify_mod = _load_verify_mod()
    load_golden_set = verify_mod.load_golden_set
    embed_query = verify_mod.embed_query
    knn_search = verify_mod.knn_search
    bm25_search = verify_mod.bm25_search
    index_name = verify_mod.INDEX_NAME

    judge_mod = _load_judge_mod()
    JudgeCache = judge_mod.JudgeCache  # noqa: N806  (type alias, not a class)
    JudgeKey = judge_mod.JudgeKey  # noqa: N806

    # ── Gate：status check（load_golden_set 內部 exit 1，不發任何外部呼叫）──
    golden = load_golden_set(GOLDEN_SET_PATH)
    queries = golden.get("queries", [])
    print(f"Golden set loaded：{len(queries)} 條查詢")

    # ── OpenSearch client（加 timeout/retry）──
    os_client = OpenSearch(
        hosts=[OS_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True,
    )

    # ── Phase 1：對每條 query 取三組 top-10，同時建 source_map（只算一次）──
    # hybrid：打運行中 app /search（mock OFF，app 自行做 embed + RRF）
    # k-NN-only：直打 OpenSearch（embed_query + knn_search）
    # BM25-only：直打 OpenSearch（bm25_search，無 embed 費用）
    print("\n[Phase 1] 取三路 top-10（hybrid / knn / bm25）…")
    query_results: list[dict] = []

    for q in queries:
        qid = q["id"]
        query_text = q["query"]
        category = q["category"]

        print(f"  [{qid}] query：{query_text!r}")

        # hybrid：打 app /search，取 mart_id list（app 端 embed + RRF，無需本地 embed）
        print(f"    → hybrid（app /search）…")
        hybrid_mart_ids = _fetch_hybrid_results(query_text, size=SEARCH_K)

        # k-NN-only：本地 embed + 直打 OpenSearch
        print(f"    → k-NN embed…")
        vector = embed_query(query_text)
        knn_hits = knn_search(os_client, vector, k=SEARCH_K)

        # BM25-only：直打 OpenSearch（零 Bedrock）
        bm25_hits = bm25_search(os_client, query_text, k=SEARCH_K)

        # source_map：三路聯集（hybrid ∪ knn ∪ bm25）皆有完整 _source，Phase 2 與 Phase 3 共用。
        # 1. 先從兩路 hits 建基礎 map（零額外查詢）
        source_map = _build_source_map(knn_hits + bm25_hits)
        # 2. mget 補抓 hybrid-only doc 的 _source（避免 judge 看到空白商品名 → artifact）
        #    hybrid 候選可能來自候選窗深位（rank 11+），不在 knn/bm25 top-10 中。
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
                "hybrid_mart_ids": hybrid_mart_ids,   # list[str]，按 RRF 分降序
                "knn_hits": knn_hits,                  # list[dict]，含 _id/_score/_source
                "bm25_hits": bm25_hits,                # list[dict]，含 _id/_score/_source
                "source_map": source_map,              # 三路聯集 martId→_source（mget 已補全）
            }
        )

    # ── Phase 2：收集三路聯集，同一輪同 judge 評相關性（JudgeCache 去重）──
    # 同一 (query_id, mart_id) 只 judge 一次，避免跨路漂移。
    # Phase 1 已用 mget 補全 hybrid-only doc 的 _source，確保每筆 judge item 都有完整商品資訊。
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
        source_map = qr["source_map"]  # 重用 Phase 1 已算好的

        # 三路聯集（hybrid + knn + bm25），by martId 去重
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

    # ── Phase 3：計算每 query 指標（hybrid_rel@10 / knn_rel@10 / bm25_rel@10）──
    print("[Phase 3] 計算三路指標 & 輸出報告…")

    per_query_metrics: list[dict] = []
    lines: list[str] = []

    # 報告標頭
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

    # 全局聚合計數器
    total_hybrid_rel = 0
    total_knn_rel = 0
    total_bm25_rel = 0

    # 互補保留追蹤：{qid: hybrid_rel_count}
    complement_check: dict[str, int] = {}

    for qr in query_results:
        qid = qr["qid"]
        query_text = qr["query_text"]
        category = qr["category"]
        hybrid_mart_ids = qr["hybrid_mart_ids"]
        knn_hits = qr["knn_hits"]
        bm25_hits = qr["bm25_hits"]
        source_map = qr["source_map"]  # 重用 Phase 1 已算好的，不重算

        # hybrid_rel@10：hybrid top-10 中 relevant 數量
        hybrid_rel_count = sum(
            1 for mid in hybrid_mart_ids
            if judge_cache.get((qid, mid), {}).get("relevant", False)
        )
        # knn_rel@10：k-NN top-10 中 relevant 數量
        knn_rel_count = sum(
            1 for h in knn_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
        )
        # bm25_rel@10：BM25 top-10 中 relevant 數量
        bm25_rel_count = sum(
            1 for h in bm25_hits
            if judge_cache.get((qid, str(h["_id"])), {}).get("relevant", False)
        )

        # 全局累計
        total_hybrid_rel += hybrid_rel_count
        total_knn_rel += knn_rel_count
        total_bm25_rel += bm25_rel_count

        # 互補保留追蹤
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

        # ── 每 query 報告區塊 ──
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

    # ── 每 query 彙總表 ──
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

    # 成功標準 (a)：全局 hybrid ≥ max(knn, bm25)
    criterion_a_pass = total_hybrid_rel >= max(total_knn_rel, total_bm25_rel)
    lines.append("### 成功標準判定\n")
    lines.append(
        f"> **(a) 全局：hybrid 總相關數 ≥ max(knn-only, bm25-only)**  \n"
        f">  \n"
        f"> hybrid={total_hybrid_rel} vs max(knn={total_knn_rel}, bm25={total_bm25_rel})={max(total_knn_rel, total_bm25_rel)}  \n"
        f"> **{'達成 ✅' if criterion_a_pass else '未達 ❌'}**\n"
    )

    # 成功標準 (b)：互補保留——向量強項與 BM25 強項 hybrid 均不歸零
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

    # 整體判定
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
