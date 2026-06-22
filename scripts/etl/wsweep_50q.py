"""Phase 2c-1 Step 4b: sweep w_bm25 over the 50-query golden set to confirm whether w=0.7 holds up (and isn't an isolated spike).

Purpose (plan Phase 2c-1 foundation):
  Bootstrap already showed the hybrid−bm25 margin is not significant over the 50 queries (CI crosses 0). This script answers the second question:
  is prod's w_bm25=0.7 an "isolated spike swept out" of the 50 queries (= overfitting), or does it sit within a flat plateau?
  Approach: use prod's min_max_score_fusion to re-fuse offline at candidate_k=20, sweep w_bm25∈{0.5..0.9},
  and compare each one's global rel@10. Flat → 0.7 isn't special, tuning w is pointless (corroborates bootstrap); sharp peak → overfitting does exist.

Cost control (safety.md §1):
  Maximize reuse — seed labels come from the ✓/✗ in the Step 3 report (out/search_eval_hybrid_50q_*.md),
  and the Opus judge is only invoked for gap pairs newly entering the top-10 in the w-sweep that haven't been judged yet. The judge model matches Step 3 (opus-4-8).

Usage:
  JUDGE_MODEL_ID=jp.anthropic.claude-opus-4-8 uv run python scripts/etl/wsweep_50q.py
  (the app doesn't need to be running; this hits OpenSearch directly + calls the Bedrock judge directly)
"""
from __future__ import annotations

import importlib.util
import os

# match the Step 3 judge model (must be set before loading the judge module, which reads env at import time)
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
CANDIDATE_K = 20  # prod candidate_multiplier(2) x size(10), matching prod's fusion candidate window
W_GRID = [0.5, 0.6, 0.7, 0.8, 0.9]
TOP_K = 10

# prod fusion function (no divergence)
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "src"))
from search_engine.fusion import min_max_score_fusion  # noqa: E402

# report-table regex (copied from investigate_hybrid_fusion.py, proven)
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
    """Parse the Step 3 three-column report into (qid, mid) → {relevant, reason} (seed labels)."""
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
    """{qid: {category, knn:[{mid,score,martName,feature}], bm25:[...]}}, persisted to a cache."""
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

    # per-query top-10 (list of ids) for each condition
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

    # collect the pairs needing labels, subtract the seed, judge the gap
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

    # compute global + per-category rel@10 for each condition
    cats = {q["id"]: q["category"] for q in queries}

    def rel_totals(per_q: dict[str, list[str]]) -> dict[str, int]:
        out = {"all": 0, "lexical_overlap": 0, "non_overlap": 0}
        for qid, top10 in per_q.items():
            n = sum(1 for mid in top10 if cache.get((qid, mid), {}).get("relevant"))
            out["all"] += n
            out[cats[qid]] += n
        return out

    results = {name: rel_totals(pq) for name, pq in conditions.items()}

    # report
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
