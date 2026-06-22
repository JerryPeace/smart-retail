"""Promo Forecast POC Dry Run.

Run one round of the R8 upgraded cross-category opportunity detection from a desktop xlsx.
No LLM calls, no S3 writes, pure deterministic ETL + reasoning chain.

Usage:
    uv run python scripts/promo_forecast_dry_run.py [--month 2026-04] [--xlsx /path/to/xlsx]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Add src to sys.path (for running as a standalone script)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from recommender.services.promo_forecast_service import (
    CrossCategoryOpportunity,
    PromoForecastService,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default="2026-04")
    parser.add_argument(
        "--xlsx",
        default="data/monthly_dealer_report.xlsx",
        help="新檔 104e 客戶別 xlsx 路徑",
    )
    parser.add_argument(
        "--out-dir",
        default=str(PROJECT_ROOT / "out"),
        help="CSV 輸出目錄",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        print(f"❌ xlsx 不存在: {xlsx_path}")
        return 1

    print(f"📥 讀取 xlsx: {xlsx_path.name} > {args.month}")
    print(f"🌐 將 batch 查 33 家經濟部所營事業 (~10 秒, friendly delay 0.3s/家)")
    print()

    service = PromoForecastService(s3=None)  # POC dry run doesn't use S3
    summary = await service.run_from_local_xlsx(args.month, xlsx_path)

    print("=" * 80)
    print(f"📊 結果摘要 — {args.month}")
    print("=" * 80)
    print(f"  專戶業務課活躍經銷商: {summary['dealer_count']} 家")
    print(f"  Cross-Sell 機會總數: {summary['opportunities_count']} 條")
    print()
    print(f"  按優先級分布:")
    for p, n in summary["by_priority"].items():
        print(f"    {p}: {n} 條")
    print()
    print(f"  按品類分布:")
    for cat, n in summary["by_category"].items():
        bar = "█" * n if n > 0 else "(無)"
        print(f"    {cat}: {n} 條  {bar}")
    print()

    print("=" * 80)
    print(f"📝 P1 (保健) 機會詳細 — 前 10 條範例")
    print("=" * 80)
    p1_opps = [o for o in summary["opportunities"] if o["priority"] == "P1"][:10]
    for i, o in enumerate(p1_opps, 1):
        print(f"\n[{i}] {o['dealer_id']} {o['dealer_name']} (業務員 {o['sales_rep']})")
        print(f"    上月業績: {o['monthly_total_ap']:,} 元")
        print(f"    推薦: {o['target_category']} | 信心度: {o['reasoning']['confidence']}")
        evidence_codes = [e['code'] + ' ' + e['description'] for e in o['moea_evidence'][:3]]
        print(f"    經濟部證據: {' / '.join(evidence_codes)}")
        print(f"    Logic: {o['reasoning']['logic']}")

    # Write CSV
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"promo_forecast_{args.month}.csv"

    opps_objs = [CrossCategoryOpportunity(**o) for o in summary["opportunities"]]
    df = PromoForecastService.opportunities_to_dataframe(opps_objs)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")  # BOM for Excel
    print(f"\n✓ CSV 已寫入: {csv_path}")
    print(f"  行數: {len(df)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
