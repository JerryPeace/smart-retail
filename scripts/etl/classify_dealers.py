"""
Tier 1 ETL: tier dealers by this month's transaction volume from the company's performance-tracking xlsx "同期by經銷商" sheet.

Input   : aws-s3/績效追蹤{月}.xlsx
Output  : out/dealer_classification_{YYYY-MM}.csv  + printed upgrade/downgrade list

Tiering rules (simplified version, using this month rather than a trailing-3-month average):
  S: this month >= 500K
  A: 100K-500K
  B: 30K-100K
  C: < 30K

Tier change: compare (this month) vs (same period last month) to find upgraded/downgraded dealers.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# ---------- column mapping ----------
# net-sales col indexes for the 7 categories. pattern: this-month col + 1 = same-period-last-month col
CATEGORY_THIS_MONTH_COLS = [6, 34, 38, 42, 46, 50, 54]
CATEGORY_LAST_MONTH_COLS = [c + 1 for c in CATEGORY_THIS_MONTH_COLS]

REGION_MAP: dict[str, str] = {
    "北區通路課": "北",
    "中區通路課": "中",
    "南區通路課": "南",
    "專戶業務課": "專戶",
    "企業客戶業務處": "專戶",
}

# tier thresholds (NTD)
TIER_THRESHOLDS = [
    (500_000, "S"),
    (100_000, "A"),
    (30_000, "B"),
    (0, "C"),
]
TIER_WEIGHT = {"S": 4, "A": 3, "B": 2, "C": 1}

# corresponding actions (official PO rules, finalized 2026-05-06)
DEFAULT_CHANNEL: dict[str, str] = {
    "S": "業務電話 + 客製 EDM",
    "A": "標準 EDM + LINE",
    "B": "群發 EDM",
    "C": "季度喚醒",
}

DATA_START_ROW = 5
REGION_COL = 0
DEALER_ID_COL = 1
DEALER_NAME_COL = 4


def classify(amount: float) -> str:
    for threshold, tier in TIER_THRESHOLDS:
        if amount >= threshold:
            return tier
    return "C"


def sum_cols(row: pd.Series, cols: list[int]) -> float:
    return sum(float(row[c]) for c in cols if pd.notna(row[c]))


def load_dealer_totals(xlsx_path: Path) -> pd.DataFrame:
    df = pd.read_excel(xlsx_path, sheet_name="同期by經銷商", header=None)
    df = df.iloc[DATA_START_ROW:].reset_index(drop=True)

    rows: list[dict] = []
    for _, row in df.iterrows():
        region_raw = row[REGION_COL]
        dealer_id = row[DEALER_ID_COL]
        if pd.isna(region_raw) or pd.isna(dealer_id):
            continue
        region = REGION_MAP.get(str(region_raw).strip())
        if not region:
            continue
        rows.append(
            {
                "dealer_id": int(dealer_id),
                "dealer_name": str(row[DEALER_NAME_COL]).strip() if pd.notna(row[DEALER_NAME_COL]) else f"#{int(dealer_id)}",
                "region": region,
                "this_month_amount": sum_cols(row, CATEGORY_THIS_MONTH_COLS),
                "last_month_amount": sum_cols(row, CATEGORY_LAST_MONTH_COLS),
            }
        )

    if not rows:
        return pd.DataFrame(rows)

    raw = pd.DataFrame(rows)
    multi_region = (raw.groupby("dealer_id")["region"].nunique() > 1).sum()
    if multi_region:
        print(f"⚠️  {multi_region} 個經銷商出現在多區域 (合併後以首次出現區域為主)")

    return (
        raw.groupby("dealer_id", as_index=False)
        .agg(
            {
                "dealer_name": "first",
                "region": "first",
                "this_month_amount": "sum",
                "last_month_amount": "sum",
            }
        )
    )


def main() -> None:
    xlsx = Path("aws-s3/sales/2026/04/績效追蹤4月.xlsx")
    out_path = Path("out/dealer_classification_2026-04.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"📥 讀取 {xlsx} > 同期by經銷商")
    totals = load_dealer_totals(xlsx)
    print(f"   經銷商總數 (有任一月成交): {len(totals)}")

    totals["this_tier"] = totals["this_month_amount"].apply(classify)
    totals["last_tier"] = totals["last_month_amount"].apply(classify)

    active = totals[totals["this_month_amount"] > 0].copy()
    active["channel"] = active["this_tier"].map(DEFAULT_CHANNEL)
    active["tax_id"] = ""  # left blank during the POC phase; pending IT providing the customer master to fill in the 8-digit tax ID

    main_table = (
        active[["dealer_name", "tax_id", "region", "this_month_amount", "this_tier", "channel"]]
        .rename(
            columns={
                "dealer_name": "客戶名稱",
                "tax_id": "統編",
                "region": "區域",
                "this_month_amount": "當月成交",
                "this_tier": "層級",
                "channel": "建議溝通管道",
            }
        )
        .sort_values("當月成交", ascending=False)
        .reset_index(drop=True)
    )
    main_table["當月成交"] = main_table["當月成交"].astype(int)
    main_table.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"📤 主表寫出 {out_path} ({len(main_table)} 行)")

    summary = (
        active.groupby("this_tier")
        .agg(經銷商數=("dealer_id", "count"), 金額總和=("this_month_amount", "sum"))
        .reindex(["S", "A", "B", "C"])
        .fillna(0)
        .astype(int)
    )
    print("\n📈 當月層級分布:")
    print(summary.to_string())

    changed = totals[
        (totals["this_tier"] != totals["last_tier"])
        & (totals["this_month_amount"] > 0)
        & (totals["last_month_amount"] > 0)
    ].copy()
    changed["change"] = changed.apply(
        lambda r: "升級" if TIER_WEIGHT[r["this_tier"]] > TIER_WEIGHT[r["last_tier"]] else "降級",
        axis=1,
    )
    print(f"\n📊 層級變動清單 ({len(changed)} 個經銷商, 排除無交易月):")
    print(
        changed[["dealer_name", "region", "last_month_amount", "last_tier", "this_month_amount", "this_tier", "change"]]
        .sort_values(["change", "this_month_amount"], ascending=[True, False])
        .to_string(index=False)
    )


if __name__ == "__main__":
    main()
