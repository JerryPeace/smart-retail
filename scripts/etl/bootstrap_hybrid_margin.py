"""Phase 2c-1 統計層：對 hybrid−bm25 / hybrid−knn 的 rel@10 margin 做配對 bootstrap CI.

目的（design：plan Phase 2c-1 地基）：
  Phase 2 在 15 條 golden set 上得「hybrid 79 > bm25 76」，差距僅 3 個 doc、樣本小。
  本腳本在擴充後的 50 條上，用配對 bootstrap 量化「hybrid > bm25」是真訊號還是雜訊：
  若 95% CI 下界 > 0 → 結論統計顯著；若 CI 跨 0 → 達標宣告需降級為「不劣於」。

輸入：
  Step 3 端到端報告 MD（judge_hybrid_search.py 產出），解析其「每 Query 指標彙總」表
  （| qid | category | hybrid_rel@10 | knn_rel@10 | bm25_rel@10 | … |）。
  ⚠️ 純後處理，0 Bedrock 呼叫、0 額外成本。

方法：
  配對 bootstrap（resample query index with replacement，hybrid/bm25/knn 取自同一條 query，
  保留配對結構）。B=10000，numpy 固定 seed 確保可重現。
  輸出全局 + 分類別（lexical_overlap / non_overlap）的 margin 點估計、95% percentile CI、P(margin>0)。

用法：
  uv run python scripts/etl/bootstrap_hybrid_margin.py [報告路徑]
  （預設讀 out/search_eval_hybrid_50q_20260613.md）
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

# 解析彙總表列：| q07 | non_overlap | 8 | 5 | 6 | … |
ROW_RE = re.compile(
    r"^\|\s*(q\d+)\s*\|\s*(lexical_overlap|non_overlap)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
)


def parse_report(path: Path) -> list[dict]:
    """從報告 MD 解析每 query 的 rel@10。

    Returns:
        [{"qid", "category", "hybrid", "knn", "bm25"}, …]，依報告順序。
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
    """配對 bootstrap：margin = sum(a) − sum(b)，resample query index。

    Args:
        a, b: 等長 per-query rel@10 陣列（同序，配對）。
        rng:  固定 seed 的 numpy generator。

    Returns:
        {"point", "ci_low", "ci_high", "p_gt0", "mean_per_q"}。
    """
    n = len(a)
    point = float(a.sum() - b.sum())
    idx = rng.integers(0, n, size=(B, n))  # B 次重抽樣，每次 n 個 index
    diffs = (a[idx] - b[idx]).sum(axis=1)  # 每次 bootstrap 的 margin（總和）
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

    # 全局 + 分類別
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
