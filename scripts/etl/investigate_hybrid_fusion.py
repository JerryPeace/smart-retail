"""
一次性調查腳本：為什麼 hybrid（naive RRF k=60）輸給 BM25-only？哪種融合策略能贏？

方法
----
1. 解析 out/search_eval_hybrid_20260613.md 的 277 個已判定 (query, doc) label 作為種子快取
   （reason 含「空白」的 label 是評估 artifact——judge 當時看不到商品資訊——標記失效重判）。
2. 對 15 條 golden query 取 k-NN top-30 與 BM25 top-30（Titan embed 15 次 + OpenSearch 30 查詢）。
3. 純 Python 建立所有融合策略的 top-10（ranking 不需要 label）：
   - RRF k-sweep（k=1/5/10/20/30/60/100, pool=20）
   - 候選池 sweep（pool=10/20/30 × k=10/60）
   - weighted RRF（w_bm25 = 0.5~0.9 × k=10/60, pool=20）
   - score-based fusion（min-max 正規化 raw _score 加權, pool=20）
   - oracle（per-query 取單路較優者；上界參考）
4. 收集所有策略 top-10 + 兩條 baseline top-10 的 (query, doc) 聯集，扣掉有效快取，
   只對新 pair 打 Opus judge（重用 judge_search_relevance 的 _judge_batch 引擎）。
5. 對照完整 label 表計算各策略全局 rel@10；q05/q08/q11 逐條診斷 gold 在融合後的去向。

用法
----
uv run python scripts/etl/investigate_hybrid_fusion.py
（冪等：runs 與 judge 結果落地 out/investigate_*.json，重跑不重費）

安全
----
打真 Bedrock（Titan embed 15 次 + Opus judge 僅新 pair），使用者已同意（公司出錢、控制用量）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# Opus judge（與前輪報告同一 judge，避免跨輪漂移）；需在載入 judge 模組前設定
os.environ.setdefault("JUDGE_MODEL_ID", "jp.anthropic.claude-opus-4-8")

SCRIPT_DIR = Path(__file__).parent
REPORT_PATH = Path("out/search_eval_hybrid_20260613.md")
RUNS_CACHE = Path("out/investigate_runs_20260613.json")
JUDGE_CACHE_PATH = Path("out/investigate_judge_cache_20260613.json")
OUT_MD = Path("out/hybrid_fusion_investigation_20260613.md")

POOL_MAX = 30  # 每路抓 top-30
FOCUS_QIDS = ["q05", "q08", "q11"]


def _load_mod(name: str):
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------- Step 1：解析既有報告 → 種子 label 快取 ----------

ROW5_RE = re.compile(r"^\|\s*(\d+) \| (\d+) \| (.*) \| ([\d.]+) \| ([✓✗]) ?(.*?) \|$")
ROW4_RE = re.compile(r"^\|\s*(\d+) \| (\d+) \| (.*) \| ([✓✗]) ?(.*?) \|$")
QID_RE = re.compile(r"^## (q\d+)「(.+)」 \((\w+)\)")


def parse_report(path: Path):
    """解析三欄報告 → (labels, report_lists, blank_keys)。

    labels: {(qid, mid): {"relevant": bool, "reason": str}}
    report_lists: {qid: {"hybrid": [...], "knn": [...], "bm25": [...]}}（top-10 mid 順序）
    blank_keys: 「商品資訊空白」artifact 的 key 集合（label 無效需重判）
    """
    labels: dict = {}
    report_lists: dict = defaultdict(lambda: {"hybrid": [], "knn": [], "bm25": []})
    blank_keys: set = set()
    conflicts = []

    qid, mode = None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = QID_RE.match(line)
        if m:
            qid = m.group(1)
            continue
        if line.startswith("### Hybrid"):
            mode = "hybrid"
            continue
        if line.startswith("### k-NN-only"):
            mode = "knn"
            continue
        if line.startswith("### BM25-only"):
            mode = "bm25"
            continue
        if line.startswith("---"):
            qid = mode = None
            continue
        if not (qid and mode and line.startswith("|")):
            continue

        m = ROW5_RE.match(line) if mode in ("knn", "bm25") else ROW4_RE.match(line)
        if not m:
            continue
        mid = m.group(2)
        mark = m.group(5) if mode in ("knn", "bm25") else m.group(4)
        reason = (m.group(6) if mode in ("knn", "bm25") else m.group(5)).strip()
        relevant = mark == "✓"

        report_lists[qid][mode].append(mid)
        key = (qid, mid)
        if key in labels and labels[key]["relevant"] != relevant:
            conflicts.append(key)
        labels[key] = {"relevant": relevant, "reason": reason}
        if "空白" in reason:
            blank_keys.add(key)

    if conflicts:
        print(f"[WARN] 報告內同 (qid,mid) label 衝突：{conflicts}", file=sys.stderr)
    return labels, dict(report_lists), blank_keys


# ---------- Step 2：抓 k-NN/BM25 top-30 ----------


def fetch_runs(queries: list[dict], verify_mod) -> dict:
    """{qid: {"query": str, "knn": [hit_slim...], "bm25": [hit_slim...]}}，落地快取。"""
    if RUNS_CACHE.exists():
        print(f"[runs] 重用快取 {RUNS_CACHE}")
        return json.loads(RUNS_CACHE.read_text(encoding="utf-8"))

    from opensearchpy import OpenSearch  # noqa: PLC0415

    client = OpenSearch(hosts=["http://localhost:9200"], timeout=60, max_retries=3,
                        retry_on_timeout=True)
    runs: dict = {}
    n_embed = 0
    for q in queries:
        qid, text = q["id"], q["query"]
        print(f"[runs] {qid} embed + top-{POOL_MAX} …")
        vector = verify_mod.embed_query(text)
        n_embed += 1
        knn_hits = verify_mod.knn_search(client, vector, k=POOL_MAX)
        bm25_hits = verify_mod.bm25_search(client, text, k=POOL_MAX)

        def slim(h):
            src = h.get("_source") or {}
            return {
                "mid": str(h["_id"]),
                "score": float(h.get("_score", 0.0)),
                "martName": src.get("martName", ""),
                "feature": src.get("feature", ""),
            }

        runs[qid] = {
            "query": text,
            "category": q["category"],
            "knn": [slim(h) for h in knn_hits],
            "bm25": [slim(h) for h in bm25_hits],
        }
    runs["_meta"] = {"titan_embed_calls": n_embed}
    RUNS_CACHE.write_text(json.dumps(runs, ensure_ascii=False), encoding="utf-8")
    print(f"[runs] 寫出 {RUNS_CACHE}（Titan embed {n_embed} 次）")
    return runs


# ---------- Step 3：融合策略（純 rank/score 計算，不需 label）----------


def weighted_rrf(knn_ids: list[str], bm25_ids: list[str], k: int,
                 w_knn: float, w_bm25: float) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    for rank, mid in enumerate(knn_ids, start=1):
        scores[mid] += w_knn / (k + rank)
    for rank, mid in enumerate(bm25_ids, start=1):
        scores[mid] += w_bm25 / (k + rank)
    return [mid for mid, _ in sorted(scores.items(), key=lambda x: (-x[1], x[0]))]


def minmax_fusion(knn_hits: list[dict], bm25_hits: list[dict],
                  w_knn: float, w_bm25: float) -> list[str]:
    def norm(hits):
        if not hits:
            return {}
        vals = [h["score"] for h in hits]
        lo, hi = min(vals), max(vals)
        if hi == lo:
            return {h["mid"]: 1.0 for h in hits}
        return {h["mid"]: (h["score"] - lo) / (hi - lo) for h in hits}

    nk, nb = norm(knn_hits), norm(bm25_hits)
    scores: dict[str, float] = defaultdict(float)
    for mid, v in nk.items():
        scores[mid] += w_knn * v
    for mid, v in nb.items():
        scores[mid] += w_bm25 * v
    return [mid for mid, _ in sorted(scores.items(), key=lambda x: (-x[1], x[0]))]


def build_strategies(runs: dict) -> dict[str, dict[str, list[str]]]:
    """{strategy_name: {qid: fused_top10_ids}}"""
    strategies: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for qid, r in runs.items():
        if qid == "_meta":
            continue
        knn_ids_full = [h["mid"] for h in r["knn"]]
        bm25_ids_full = [h["mid"] for h in r["bm25"]]

        strategies["baseline_knn"][qid] = knn_ids_full[:10]
        strategies["baseline_bm25"][qid] = bm25_ids_full[:10]

        # RRF k-sweep（pool=20，對齊 prod candidate_k=2×size）
        for k in (1, 5, 10, 20, 30, 60, 100):
            name = f"rrf_k{k}_pool20"
            strategies[name][qid] = weighted_rrf(
                knn_ids_full[:20], bm25_ids_full[:20], k, 1.0, 1.0)[:10]

        # 候選池 sweep
        for pool in (10, 30):
            for k in (10, 60):
                name = f"rrf_k{k}_pool{pool}"
                strategies[name][qid] = weighted_rrf(
                    knn_ids_full[:pool], bm25_ids_full[:pool], k, 1.0, 1.0)[:10]

        # weighted RRF（w_bm25 sweep；w_knn=1-w_bm25）
        for k in (10, 60):
            for wb in (0.3, 0.6, 0.7, 0.8, 0.9):
                name = f"wrrf_k{k}_b{int(wb*100)}_pool20"
                strategies[name][qid] = weighted_rrf(
                    knn_ids_full[:20], bm25_ids_full[:20], k, 1.0 - wb, wb)[:10]

        # score-based fusion（min-max raw _score, pool=20）
        for wb in (0.3, 0.5, 0.7):
            name = f"minmax_b{int(wb*100)}_pool20"
            strategies[name][qid] = minmax_fusion(
                r["knn"][:20], r["bm25"][:20], 1.0 - wb, wb)[:10]

    return dict(strategies)


# ---------- Step 4：judge 補判（只判新 pair）----------


def load_persisted_judge() -> dict:
    if JUDGE_CACHE_PATH.exists():
        raw = json.loads(JUDGE_CACHE_PATH.read_text(encoding="utf-8"))
        return {tuple(k.split("|", 1)): v for k, v in raw.items()}
    return {}


def save_persisted_judge(cache: dict) -> None:
    raw = {f"{k[0]}|{k[1]}": v for k, v in cache.items()}
    JUDGE_CACHE_PATH.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


# ---------- 主流程 ----------


def main() -> None:
    verify_mod = _load_mod("verify_search_os")
    judge_mod = _load_mod("judge_search_relevance")
    print(f"judge 模型：{judge_mod.JUDGE_MODEL_ID}")

    golden = verify_mod.load_golden_set(SCRIPT_DIR / "golden_set_product_search.yaml")
    queries = golden["queries"]

    # Step 1：種子 label
    seed_labels, report_lists, blank_keys = parse_report(REPORT_PATH)
    print(f"[seed] 報告解析：{len(seed_labels)} 個 label，"
          f"其中 {len(blank_keys)} 個為「商品資訊空白」artifact（標記失效重判）")

    # Step 2：top-30 runs
    runs = fetch_runs(queries, verify_mod)

    # 一致性檢查：新抓 top-10 vs 報告 top-10；重現 hybrid（rrf k=60 pool 20）
    print("\n[consistency] 新抓結果 vs 報告：")
    mismatch = 0
    for qid in report_lists:
        new_knn10 = [h["mid"] for h in runs[qid]["knn"][:10]]
        new_bm2510 = [h["mid"] for h in runs[qid]["bm25"][:10]]
        repro_hybrid = weighted_rrf(
            [h["mid"] for h in runs[qid]["knn"][:20]],
            [h["mid"] for h in runs[qid]["bm25"][:20]], 60, 1.0, 1.0)[:10]
        ok_k = new_knn10 == report_lists[qid]["knn"]
        ok_b = new_bm2510 == report_lists[qid]["bm25"]
        ok_h = repro_hybrid == report_lists[qid]["hybrid"]
        if not (ok_k and ok_b and ok_h):
            mismatch += 1
            print(f"  [{qid}] knn一致={ok_k} bm25一致={ok_b} hybrid重現={ok_h}")
    if mismatch == 0:
        print("  全部 15 條：knn/bm25 top-10 與報告一致，hybrid top-10 純 Python 重現成功")

    # Step 3：策略 ranking
    strategies = build_strategies(runs)
    print(f"\n[strategies] 共 {len(strategies)} 個策略（含 2 條 baseline）")

    # Step 4：收集需要 label 的 (qid, mid) 聯集
    needed: set = set()
    for per_q in strategies.values():
        for qid, top10 in per_q.items():
            for mid in top10:
                needed.add((qid, mid))
    # 診斷需要：focus query 的 prod-config 融合 top-20 全列
    for qid in FOCUS_QIDS:
        fused20 = weighted_rrf(
            [h["mid"] for h in runs[qid]["knn"][:20]],
            [h["mid"] for h in runs[qid]["bm25"][:20]], 60, 1.0, 1.0)[:20]
        for mid in fused20:
            needed.add((qid, mid))

    valid_seed = {k: v for k, v in seed_labels.items() if k not in blank_keys}
    persisted = load_persisted_judge()
    labels: dict = {}
    labels.update(valid_seed)
    labels.update(persisted)  # 本腳本先前已判的（含 blank 重判）

    pending_keys = sorted(needed - set(labels))
    # source lookup：mid → (martName, feature)（同 query 兩路任一有 source 即可）
    src_lookup: dict = {}
    for qid, r in runs.items():
        if qid == "_meta":
            continue
        for h in r["knn"] + r["bm25"]:
            src_lookup[(qid, h["mid"])] = (h["martName"], h["feature"])

    judge_items = []
    no_source = []
    qtext = {q["id"]: q["query"] for q in queries}
    for key in pending_keys:
        qid, mid = key
        name, feature = src_lookup.get(key, ("", ""))
        if not name:
            no_source.append(key)
        judge_items.append((key, qtext[qid], name, feature))

    print(f"\n[judge] 重用有效種子 label {len(valid_seed)} 個"
          f"（report 277 中失效 {len(blank_keys)} 個）+ 本地已判 {len(persisted)} 個")
    print(f"[judge] 需新判 {len(judge_items)} 個 (query, doc) pair"
          f"（其中 {len(no_source)} 個無 source，將以空資訊判定）")
    if no_source:
        print(f"        無 source 的 pair：{no_source}")

    if os.environ.get("DRY_RUN") == "1":
        print("[DRY_RUN] 停在 judge 前（未發任何 Opus 呼叫）")
        return

    if judge_items:
        new_cache: dict = {}
        judge_mod._judge_batch(judge_items, new_cache)
        labels.update(new_cache)
        persisted.update(new_cache)
        save_persisted_judge(persisted)
        print(f"[judge] 完成 {len(new_cache)} 次 Opus 呼叫，已落地 {JUDGE_CACHE_PATH}")

    # Step 5：計分
    def rel_at_10(qid: str, top10: list[str]) -> int:
        return sum(1 for mid in top10
                   if labels.get((qid, mid), {}).get("relevant", False))

    qids = [q["id"] for q in queries]
    rows = []
    for name, per_q in strategies.items():
        per_query = {qid: rel_at_10(qid, per_q[qid]) for qid in qids}
        rows.append((name, sum(per_query.values()), per_query))

    knn_total = next(t for n, t, _ in rows if n == "baseline_knn")
    bm25_total = next(t for n, t, _ in rows if n == "baseline_bm25")

    # oracle：per-query 取兩條 baseline 較優者（路由上界）
    oracle_per_q = {}
    for qid in qids:
        oracle_per_q[qid] = max(
            rel_at_10(qid, strategies["baseline_knn"][qid]),
            rel_at_10(qid, strategies["baseline_bm25"][qid]))
    rows.append(("oracle_route(knn|bm25)", sum(oracle_per_q.values()), oracle_per_q))

    rows.sort(key=lambda r: -r[1])

    # 修正 label 後的 prod hybrid（artifact 量化）
    prod_per_q = {qid: rel_at_10(qid, strategies["rrf_k60_pool20"][qid]) for qid in qids}
    prod_total = sum(prod_per_q.values())

    # ---------- 輸出 ----------
    out: list[str] = []
    out.append("# Hybrid 融合策略調查 — 20260613\n")
    out.append(f"> 種子 label：{len(valid_seed)}（重用報告）+ blank artifact 重判 {len(blank_keys)}；"
               f"本輪新 Opus 判定 {len(judge_items)} 次  ")
    out.append(f"> baseline（同一 label 表重算）：knn={knn_total}、bm25={bm25_total}；"
               f"報告原值 knn=65、bm25=76、hybrid=69  ")
    out.append(f"> prod 設定（rrf k=60 pool=20）修正 label 後 = {prod_total}\n")

    out.append("## 策略總表（全局 rel@10，15 query 加總）\n")
    out.append("| 策略 | 全局 rel@10 | vs bm25-only |")
    out.append("|------|:----:|:----:|")
    for name, total, _ in rows:
        delta = total - bm25_total
        mark = "**贏**" if delta > 0 else ("平" if delta == 0 else f"{delta}")
        out.append(f"| {name} | {total} | {mark} |")

    out.append("\n## 每 query 明細（重點策略）\n")
    key_strats = ["baseline_knn", "baseline_bm25", "rrf_k60_pool20", "rrf_k10_pool20",
                  "rrf_k5_pool20", "rrf_k1_pool20", "rrf_k60_pool10", "rrf_k10_pool10",
                  "wrrf_k10_b70_pool20", "wrrf_k60_b70_pool20", "minmax_b50_pool20",
                  "minmax_b70_pool20"]
    out.append("| qid | " + " | ".join(key_strats) + " |")
    out.append("|---|" + "|".join([":---:"] * len(key_strats)) + "|")
    for qid in qids:
        cells = [str(rel_at_10(qid, strategies[s][qid])) for s in key_strats]
        out.append(f"| {qid} | " + " | ".join(cells) + " |")

    # 診斷：focus query 的 prod-config 融合視圖
    out.append("\n## 病灶診斷（prod 設定 rrf k=60 pool=20 的融合視圖）\n")
    for qid in FOCUS_QIDS:
        knn_ids = [h["mid"] for h in runs[qid]["knn"][:20]]
        bm25_ids = [h["mid"] for h in runs[qid]["bm25"][:20]]
        rank_knn = {m: i + 1 for i, m in enumerate(knn_ids)}
        rank_bm25 = {m: i + 1 for i, m in enumerate(bm25_ids)}
        fused = weighted_rrf(knn_ids, bm25_ids, 60, 1.0, 1.0)
        name_of = {h["mid"]: h["martName"] for h in runs[qid]["knn"] + runs[qid]["bm25"]}

        out.append(f"\n### {qid}「{runs[qid]['query']}」\n")
        out.append("| fused rank | mid | name(前22字) | knn rank | bm25 rank | rel |")
        out.append("|---|---|---|:--:|:--:|:--:|")
        for i, mid in enumerate(fused[:20], start=1):
            lab = labels.get((qid, mid), {})
            mark = "✓" if lab.get("relevant") else "✗"
            out.append(
                f"| {i} | {mid} | {name_of.get(mid, '')[:22]} "
                f"| {rank_knn.get(mid, '—')} | {rank_bm25.get(mid, '—')} | {mark} |")

        # gold（兩條 baseline top-10 中 relevant 的 doc）在融合後的去向
        golds = [m for m in dict.fromkeys(
            strategies["baseline_knn"][qid] + strategies["baseline_bm25"][qid])
            if labels.get((qid, m), {}).get("relevant")]
        fused_pos = {m: i + 1 for i, m in enumerate(fused)}
        out.append("\ngold（單路 top-10 中 relevant）融合後排名：")
        for m in golds:
            out.append(f"- {m} {name_of.get(m, '')[:20]}："
                       f"knn r{rank_knn.get(m, '—')} / bm25 r{rank_bm25.get(m, '—')}"
                       f" → fused r{fused_pos.get(m, '>20')}")

    report = "\n".join(out)
    OUT_MD.write_text(report, encoding="utf-8")
    print(f"\n報告寫出：{OUT_MD}\n")
    print(report)


if __name__ == "__main__":
    main()
