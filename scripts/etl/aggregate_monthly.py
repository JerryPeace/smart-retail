"""
Tier 1 ETL: aggregate the company's performance-tracking xlsx "同期by經銷商" sheet into the format the prompt expects.

Input   : aws-s3/績效追蹤{月}.xlsx  (this POC hardcodes April)
Output  : out/aggregated_{YYYY-MM}.csv  (6 categories x 4 regions = 24 rows)

Strategy: algorithm-first, deterministic computation. Falls back to the LLM tier when the monthly format drifts (not yet implemented).
Usage   : uv run python scripts/etl/aggregate_monthly.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# ---------- business mapping (change the rules by adjusting these few dicts) ----------

# source sheet col index → the 6 prompt categories
# each category spans 4 columns: [this month's units, this month's net sales, same period last month's net sales, same period last year's net sales]
# we take only "this month's net sales"
CATEGORY_COL_MAP: dict[int, str] = {
    6: "通訊",   # mobile phones, this month's net sales
    34: "通訊",  # tablet category -> merged into telecom by default
    38: "資訊",  # IT category
    42: "家電",  # home appliances category
    46: "配件",  # peripherals category -> accessories
    50: "二手機",  # pre-owned/recycling category
    54: "保健",  # health & wellness
}

# section (col 0) → the 4 prompt regions
REGION_MAP: dict[str, str] = {
    "北區通路課": "北",
    "中區通路課": "中",
    "南區通路課": "南",
    "專戶業務課": "專戶",
    "企業客戶業務處": "專戶",  # merged into key accounts by default
}

PROMPT_REGIONS = ["北", "中", "南", "專戶"]
PROMPT_CATEGORIES = ["通訊", "資訊", "配件", "家電", "保健", "二手機"]

# ---------- sheet structure constants ----------
DATA_START_ROW = 5  # row 0=noise, row 1-3=multi-level header, row 4=totals row
REGION_COL = 0  # unit (section)
DEALER_ID_COL = 1  # dealer numeric ID

# ---------- main logic ----------


def load_dealer_records(xlsx_path: Path, sheet: str = "同期by經銷商") -> pd.DataFrame:
    """Read the raw sheet, skip the header + totals rows, and unpivot into a long table (region, category, dealer_id, amount)."""
    df = pd.read_excel(xlsx_path, sheet_name=sheet, header=None)
    df = df.iloc[DATA_START_ROW:].reset_index(drop=True)

    records: list[dict] = []
    skipped_unknown_region: set[str] = set()

    for _, row in df.iterrows():
        region_raw = row[REGION_COL]
        dealer_id = row[DEALER_ID_COL]
        if pd.isna(region_raw) or pd.isna(dealer_id):
            continue
        region_key = str(region_raw).strip()
        region = REGION_MAP.get(region_key)
        if not region:
            skipped_unknown_region.add(region_key)
            continue
        for col_idx, category in CATEGORY_COL_MAP.items():
            amount = row[col_idx]
            if pd.isna(amount) or float(amount) <= 0:
                continue  # zero or negative sales don't count as a record
            records.append(
                {
                    "region": region,
                    "category": category,
                    "dealer_id": int(dealer_id),
                    "amount": float(amount),
                }
            )

    if skipped_unknown_region:
        print(f"⚠️  跳過未認識的課別: {skipped_unknown_region}")

    df = pd.DataFrame(records)
    if df.empty:
        return df
    # merge multiple source columns mapping to the same (region, category, dealer) (e.g. mobile phones + tablets both count as telecom)
    return df.groupby(["region", "category", "dealer_id"], as_index=False)["amount"].sum()


def aggregate(records: pd.DataFrame) -> pd.DataFrame:
    """Aggregate to the (region, category) level and fill in zero-valued combinations."""
    grouped = (
        records.groupby(["region", "category"])
        .agg(
            total_amount=("amount", "sum"),
            tx_count=("amount", "count"),
            dealer_count=("dealer_id", "nunique"),
        )
        .reset_index()
    )

    full = pd.MultiIndex.from_product(
        [PROMPT_REGIONS, PROMPT_CATEGORIES], names=["region", "category"]
    ).to_frame(index=False)
    out = full.merge(grouped, on=["region", "category"], how="left").fillna(0)

    out["total_amount"] = out["total_amount"].astype(int)
    out["tx_count"] = out["tx_count"].astype(int)
    out["dealer_count"] = out["dealer_count"].astype(int)

    out["region"] = pd.Categorical(out["region"], PROMPT_REGIONS, ordered=True)
    out["category"] = pd.Categorical(out["category"], PROMPT_CATEGORIES, ordered=True)
    return out.sort_values(["region", "category"]).reset_index(drop=True)


def main() -> None:
    xlsx = Path("aws-s3/sales/2026/04/績效追蹤4月.xlsx")
    out_path = Path("out/aggregated_2026-04.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"📥 讀取 {xlsx} > 同期by經銷商")
    records = load_dealer_records(xlsx)
    print(f"   展開後 (dealer × category) 有效記錄: {len(records)}")
    print(f"   覆蓋 {records['dealer_id'].nunique()} 個經銷商")

    result = aggregate(records)
    result.columns = ["區域", "品類", "今日金額", "今日筆數", "客戶數"]
    result.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"\n📤 寫出 {out_path}")
    print("\n" + result.to_string(index=False))


if __name__ == "__main__":
    main()
