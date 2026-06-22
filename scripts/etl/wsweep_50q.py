"""Phase 2c-1 Step 4b：在 50 條 golden set 上掃 w_bm25，確認 w=0.7 是否站得住（非孤峰）.

目的（plan Phase 2c-1 地基）：
  bootstrap 已證明 hybrid−bm25 margin 在 50 條上不顯著（CI 跨 0）。本腳本回答第二個問題：
  prod 的 w_bm25=0.7 是不是在 50 條上「掃出來的孤峰」（=過擬合），還是在一段平坦區間裡？
  做法：用 prod 的 min_max_score_fusion 在 candidate_k=20 上離線重融合，掃 w_bm25∈{0.5..0.9}，
  比各自全局 rel@10。平坦 → 0.7 不特殊、調 w 無意義（佐證 bootstrap）；尖峰 → 確有過擬合。

成本控制（safety.md §1）：
  最大化複用——seed label 取自 Step 3 報告（out/search_eval_hybrid_50q_*.md）的 ✓/✗，
  只對 w-sweep 新進 top-10 但未判過的 gap pair 打 Opus judge。judge 模型對齊 Step 3（opus-4-8）。

用法：
  JUDGE_MODEL_ID=jp.anthropic.claude-opus-4-8 uv run python scripts/etl/wsweep_50q.py
  （app 不需啟動；直打 OpenSearch + 直呼 Bedrock judge）
"""
from __future__ import annotations

import importlib.util
import os

# 對齊 Step 3 judge 模型（須在載入 judge 模組前設定，模組於 import 時讀 env）
os.environ.setdefault("JUDGE_MODEL_ID", "jp.anthropic.claude-opus-4-8")

import re
import sys
import json
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPORT = Path("out/search_eval_hybrid_50q_20260613.md")
RUNS_CACHE = Path("out/wsweep_runs_50q.json")
OUT_MD = Path("out/phase2c1_wsweep_50q_20260613.md")
CANDIDATE_K = 20  # prod candidate_multiplier(2) × size(10)，對齊 prod 融合候選窗
W_GRID = [0.5, 0.6, 0.7, 0.8, 0.9]
TOP_K = 10

# prod 融合函式（零分歧）
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "src"))
from search_engine.fusion import min_max_score_fusion  # noqa: E402

# 報告表格 regex（複製自 investigate_hybrid_fusion.py，proven）
QID_RE = re.compile(r"^## (q\d+)「(.+)」 \((\w+)\)")
ROW5_RE = re.compile(r"^\|\s*(\d+) \| (\d+) \| (.*) \| ([\d.]+) \| ([✓✗]) ?(.*?) \|$")
ROW4_RE = re.compile(r"^\|\s*(\d+) \| (\d+) \| (.*) \| ([✓✗]) ?(.*?) \|$")


def _load(name: str):
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_report(path: Path) -> dict:
    """從 Step 3 三欄報告解析 (qid, mid) → {relevant, reason}（seed label）。"""
    labels: dict = {}
    qid = mode = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = QID_RE.match(line)
        if m:
            qid = m.group(1)
            continue
        if line.startswith("### Hybrid"):
            mode = "hybrid"; continue
        if line.startswith("### k-NN-only"):
            mode = "knn"; continue
        if line.startswith("### BM25-only"):
            mode = "bm25"; continue
        if line.startswith("---"):
            qid = mode = None; continue
        if not (qid and mode and line.startswith("|")):
            continue
        m = ROW5_RE.match(line) if mode in ("knn", "bm25") else ROW4_RE.match(line)
        if not m:
            continue
        mid = m.group(2)
        mark = m.group(5) if mode in ("knn", "bm25") else m.group(4)
        labels[(qid, mid)] = {"relevant": mark == "✓"}
    return labels


def fetch_runs(queries: list[dict], verify_mod) -> dict:
    """{qid: {category, knn:[{mid,score,martName,feature}], bm25:[...]}}，落地快取。"""
    if RUNS_CACHE.exists():
        print(f"[runs] 重用快取 {RUNS_CACHE}")
        return json.loads(RUNS_CACHE.read_text(encoding="utf-8"))

    from opensearchpy import OpenSearch  # noqa: PLC0415

    client = OpenSearch(hosts=["http://localhost:9200"], timeout=60,
                        max_retries=3, retry_on_timeout=True)
    runs: dict = {}
    for q in queries:
        qid, text = q["id"], q["query"]
        print(f"[runs] {qid} embed + top-{CANDIDATE_K} …")
        vec = verify_mod.embed_query(text)
        knn = verify_mod.knn_search(client, vec, k=CANDIDATE_K)
        bm25 = verify_mod.bm25_search(client, text, k=CANDIDATE_K)

        def slim(h):
            src = h.get("_source") or {}
            return {"mid": str(h["_id"]), "score": float(h.get("_score", 0.0)),
                    "martName": src.get("martName", ""), "feature": src.get("feature", "")}

        runs[qid] = {"category": q["category"],
                     "knn": [slim(h) for h in knn], "bm25": [slim(h) for h in bm25]}
    RUNS_CACHE.write_text(json.dumps(runs, ensure_ascii=False), encoding="utf-8")
    print(f"[runs] 寫出 {RUNS_CACHE}")
    return runs


def main() -> None:
    verify_mod = _load("verify_search_os")
    judge_mod = _load("judge_search_relevance")
    print(f"judge 模型：{judge_mod.JUDGE_MODEL_ID}")

    golden = verify_mod.load_golden_set(SCRIPT_DIR / "golden_set_product_search.yaml")
    queries = golden["queries"]
    seed = parse_report(REPORT)
    print(f"[seed] 從 Step 3 報告複用 {len(seed)} 筆判分")

    runs = fetch_runs(queries, verify_mod)

    # 各條件的 per-query top-10（id 清單）
    conditions: dict[str, dict[str, list[str]]] = {}
    for w in W_GRID:
        conditions[f"minmax_w{int(w*100)}"] = {}
    conditions["knn_only"] = {}
    conditions["bm25_only"] = {}

    src_lookup: dict = {}
    for qid, r in runs.items():
        knn_scored = [(h["mid"], h["score"]) for h in r["knn"]]
        bm25_scored = [(h["mid"], h["score"]) for h in r["bm25"]]
        for h in r["knn"] + r["bm25"]:
            src_lookup[(qid, h["mid"])] = (h["martName"], h["feature"])
        for w in W_GRID:
            fused = min_max_score_fusion(knn_scored, bm25_scored, w_bm25=w, w_knn=1 - w)
            conditions[f"minmax_w{int(w*100)}"][qid] = [mid for mid, _ in fused[:TOP_K]]
        conditions["knn_only"][qid] = [h["mid"] for h in r["knn"][:TOP_K]]
        conditions["bm25_only"][qid] = [h["mid"] for h in r["bm25"][:TOP_K]]

    # 收集需要 label 的 pair，扣掉 seed，judge gap
    needed: set = set()
    for per_q in conditions.values():
        for qid, top10 in per_q.items():
            for mid in top10:
                needed.add((qid, mid))
    cache = {k: dict(v) for k, v in seed.items()}
    gap = sorted(needed - set(cache))
    qtext = {q["id"]: q["query"] for q in queries}
    items = []
    for qid, mid in gap:
        name, feature = src_lookup.get((qid, mid), ("", ""))
        items.append(((qid, mid), qtext[qid], name, feature))
    print(f"[judge] 需新判 gap pair：{len(items)}（seed 已覆蓋 {len(needed)-len(gap)}/{len(needed)}）")
    if items:
        judge_mod._judge_batch(items, cache)

    # 計算各條件全局 + 分類別 rel@10
    cats = {q["id"]: q["category"] for q in queries}

    def rel_totals(per_q: dict[str, list[str]]) -> dict[str, int]:
        out = {"all": 0, "lexical_overlap": 0, "non_overlap": 0}
        for qid, top10 in per_q.items():
            n = sum(1 for mid in top10 if cache.get((qid, mid), {}).get("relevant"))
            out["all"] += n
            out[cats[qid]] += n
        return out

    results = {name: rel_totals(pq) for name, pq in conditions.items()}

    # 報告
    lines = [
        "# Phase 2c-1 Step 4b — w_bm25 敏感度掃描（50 條）\n",
        f"> 來源 seed：`{REPORT}`（{len(seed)} 筆複用）　|　judge：`{judge_mod.JUDGE_MODEL_ID}`　"
        f"|　candidate_k={CANDIDATE_K}（對齊 prod）　|　離線 min_max_score_fusion（prod 同函式）\n",
        "> 問題：w=0.7 是平坦區間內一點（沒過擬合）還是孤峰（過擬合）？\n",
        "\n## 全局 rel@10（w 掃描 + 單一方法對照）\n",
        "| 條件 | 全局 | lexical | non_overlap |",
        "|------|:---:|:---:|:---:|",
    ]
    order = [f"minmax_w{int(w*100)}" for w in W_GRID] + ["knn_only", "bm25_only"]
    label_map = {f"minmax_w{int(w*100)}": f"min-max w_bm25={w}" for w in W_GRID}
    label_map.update({"knn_only": "k-NN only", "bm25_only": "BM25 only"})
    for name in order:
        r = results[name]
        star = " ⭐prod" if name == "minmax_w70" else ""
        lines.append(f"| {label_map[name]}{star} | {r['all']} | {r['lexical_overlap']} | {r['non_overlap']} |")

    sweep_vals = [results[f"minmax_w{int(w*100)}"]["all"] for w in W_GRID]
    peak = max(sweep_vals)
    flat = peak - min(sweep_vals)
    verdict = (
        f"w 掃描全局 rel@10 範圍 [{min(sweep_vals)}, {peak}]，極差 {flat}。"
        + ("**平坦（極差 ≤2）→ w=0.7 非孤峰，調 w 無顯著作用，佐證 bootstrap 的「margin 不顯著」**。"
           if flat <= 2 else
           "**有起伏 → 需檢視 0.7 是否為峰值（過擬合風險）**。")
    )
    lines += ["\n## 判讀\n", verdict + "\n"]

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n✅ 報告寫入 {OUT_MD}")


if __name__ == "__main__":
    main()
