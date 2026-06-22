"""Phase 2c-1 statistics layer: compute a paired bootstrap CI for the rel@10 margin of hybrid−bm25 / hybrid−knn.

Purpose (design: plan Phase 2c-1 foundation):
  Phase 2 got "hybrid 79 > bm25 76" on the 15-query golden set — a gap of only 3 docs, on a small sample.
  This script uses a paired bootstrap on the expanded 50 queries to quantify whether "hybrid > bm25" is real signal or noise:
  if the 95% CI lower bound > 0 → the conclusion is statistically significant; if the CI crosses 0 → the success claim must be downgraded to "not inferior".

Input:
  The Step 3 end-to-end report MD (produced by judge_hybrid_search.py); parse its "per-query metrics summary" table
  (| qid | category | hybrid_rel@10 | knn_rel@10 | bm25_rel@10 | … |).
  ⚠️ Pure post-processing, 0 Bedrock calls, 0 additional cost.

Method:
  Paired bootstrap (resample query index with replacement; hybrid/bm25/knn are taken from the same query,
  preserving the paired structure). B=10000, with a fixed numpy seed for reproducibility.
  Outputs the margin point estimate, 95% percentile CI, and P(margin>0), both globally and per category (lexical_overlap / non_overlap).

Usage:
  uv run python scripts/etl/bootstrap_hybrid_margin.py [report path]
  (defaults to reading out/search_eval_hybrid_50q_20260613.md)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np

DEFAULT_REPORT = Path("out/search_eval_hybrid_50q_20260613.md")
OUT_MD = Path("out/phase2c1_bootstrap_20260613.md")
B = 10_000
SEED = 20260613
CI_LOW, CI_HIGH = 2.5, 97.5

# parse summary-table rows: | q07 | non_overlap | 8 | 5 | 6 | … |
ROW_RE = re.compile(
    r"^\|\s*(q\d+)\s*\|\s*(lexical_overlap|non_overlap)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
)


def parse_report(path: Path) -> list[dict]:
    """Parse each query's rel@10 from the report MD.

    Returns:
        [{"qid", "category", "hybrid", "knn", "bm25"}, …], in report order.
    """
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line)
        if m:
            rows.append(
                {
                    "qid": m.group(1),
                    "category": m.group(2),
                    "hybrid": int(m.group(3)),
                    "knn": int(m.group(4)),
                    "bm25": int(m.group(5)),
                }
            )
    return rows


def bootstrap_margin(
    a: np.ndarray, b: np.ndarray, rng: np.random.Generator
) -> dict:
    """Paired bootstrap: margin = sum(a) − sum(b), resampling the query index.

    Args:
        a, b: equal-length per-query rel@10 arrays (same order, paired).
        rng:  a fixed-seed numpy generator.

    Returns:
        {"point", "ci_low", "ci_high", "p_gt0", "mean_per_q"}.
    """
    n = len(a)
    point = float(a.sum() - b.sum())
    idx = rng.integers(0, n, size=(B, n))  # B resamples, each with n indexes
    diffs = (a[idx] - b[idx]).sum(axis=1)  # the margin (total) of each bootstrap
    return {
        "point": point,
        "ci_low": float(np.percentile(diffs, CI_LOW)),
        "ci_high": float(np.percentile(diffs, CI_HIGH)),
        "p_gt0": float((diffs > 0).mean()),
        "mean_per_q": point / n,
    }


def _fmt(label: str, n: int, totals: dict, m_bm25: dict, m_knn: dict) -> list[str]:
    verdict = (
        "✅ 顯著 > BM25（CI 下界 > 0）"
        if m_bm25["ci_low"] > 0
        else "⚠️ CI 跨 0：與 BM25 差異不顯著（達標應降級為「不劣於」）"
    )
    return [
        f"\n### {label}（N={n}）\n",
        f"- 全局 rel@10：hybrid **{totals['hybrid']}** / bm25 {totals['bm25']} / knn {totals['knn']}",
        f"- **hybrid − bm25** margin：點估計 **{m_bm25['point']:+.0f}**"
        f"（每 query 平均 {m_bm25['mean_per_q']:+.3f}），"
        f"95% CI [{m_bm25['ci_low']:+.1f}, {m_bm25['ci_high']:+.1f}]，"
        f"P(hybrid>bm25)={m_bm25['p_gt0']:.1%}",
        f"- hybrid − knn margin：點估計 {m_knn['point']:+.0f}，"
        f"95% CI [{m_knn['ci_low']:+.1f}, {m_knn['ci_high']:+.1f}]，"
        f"P(hybrid>knn)={m_knn['p_gt0']:.1%}",
        f"- **判讀**：{verdict}",
    ]


def main() -> None:
    report = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REPORT
    if not report.exists():
        sys.exit(f"❌ 找不到報告：{report}（先跑 Step 3 judge_hybrid_search.py）")

    rows = parse_report(report)
    if not rows:
        sys.exit(f"❌ 報告 {report} 解析不到彙總表列（格式可能變動）")

    rng = np.random.default_rng(SEED)
    lines: list[str] = [
        "# Phase 2c-1 — hybrid margin 配對 bootstrap CI\n",
        f"> 來源報告：`{report}`　|　樣本 N={len(rows)} 條 golden query　|　"
        f"B={B} resample　|　seed={SEED}（可重現）　|　0 Bedrock 成本（純後處理）\n",
        "> 配對 bootstrap：每次重抽樣 query index，hybrid/bm25/knn 取自同一條 query 保留配對結構。\n",
    ]

    # global + per category
    buckets: list[tuple[str, list[dict]]] = [
        ("全局", rows),
        ("lexical_overlap", [r for r in rows if r["category"] == "lexical_overlap"]),
        ("non_overlap", [r for r in rows if r["category"] == "non_overlap"]),
    ]
    for label, group in buckets:
        if not group:
            continue
        hyb = np.array([r["hybrid"] for r in group])
        bm = np.array([r["bm25"] for r in group])
        knn = np.array([r["knn"] for r in group])
        totals = {"hybrid": int(hyb.sum()), "bm25": int(bm.sum()), "knn": int(knn.sum())}
        m_bm25 = bootstrap_margin(hyb, bm, rng)
        m_knn = bootstrap_margin(hyb, knn, rng)
        lines += _fmt(label, len(group), totals, m_bm25, m_knn)

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n✅ 報告寫入 {OUT_MD}")


if __name__ == "__main__":
    main()
