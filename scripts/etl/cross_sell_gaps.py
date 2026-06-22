"""
Tier 1 ETL: 從 本公司 績效追蹤 xlsx 偵測經銷商交叉銷售缺口.

輸入  : aws-s3/績效追蹤{月}.xlsx
輸出  : out/cross_sell_gaps_{YYYY-MM}.csv

缺口定義 (用「當月 + 上月同期」作為近期購買 proxy):
  Rule 1: 買通訊未買配件        (高毛利缺口)
  Rule 2: 買家電未買保健        (高毛利缺口)
  Rule 3: 只買單一品類           (任何品類)
  Rule 4: 買通訊或資訊, 未買二手機

每個 (經銷商 × 命中規則) 一行, 含業務直接可用的開場白模板.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# 6 prompt 品類 -> 對應 xlsx col index 列表 (含當月 + 上月同期)
# 通訊 = 行動電話 + 平板 (合併處理)
CATEGORY_COLS: dict[str, list[int]] = {
    "通訊": [6, 7, 34, 35],
    "資訊": [38, 39],
    "配件": [46, 47],
    "家電": [42, 43],
    "保健": [54, 55],
    "二手機": [50, 51],
}

REGION_MAP: dict[str, str] = {
    "北區通路課": "北",
    "中區通路課": "中",
    "南區通路課": "南",
    "專戶業務課": "專戶",
    "企業客戶業務處": "專戶",
}

# 開場白模板 (PO 可調整)
OPENING_LINES: dict[str, str] = {
    "rule1": "{name} 老闆, 您近期通訊產品有銷售但配件這塊還沒起來. 配件毛利比通訊高 3-4 倍, 幫您看看搭售方案?",
    "rule2": "{name} 老闆, 家電有量但保健尚未開發. 保健是高毛利主推品項, 安排 30 分鐘看樣?",
    "rule3": "{name} 老闆, 目前主要採購 {existing}, 評估擴一個品類試水溫? 保健或配件都是好切入點.",
    "rule4": "{name} 老闆, 通訊/資訊客流不錯但沒做二手機回收. 增加客戶黏著 + 多一個毛利來源.",
}

DATA_START_ROW = 5
REGION_COL = 0
DEALER_ID_COL = 1
DEALER_NAME_COL = 4


def detect_gaps(xlsx_path: Path) -> pd.DataFrame:
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
        name = (
            str(row[DEALER_NAME_COL]).strip()
            if pd.notna(row[DEALER_NAME_COL])
            else f"#{int(dealer_id)}"
        )

        bought: dict[str, bool] = {}
        for cat, cols in CATEGORY_COLS.items():
            total = sum(float(row[c]) for c in cols if pd.notna(row[c]))
            bought[cat] = total > 0

        existing = [c for c, b in bought.items() if b]
        if not existing:
            continue

        existing_str = ", ".join(existing)

        if bought["通訊"] and not bought["配件"]:
            rows.append(
                {
                    "客戶名稱": name,
                    "區域": region,
                    "缺口類型": "買通訊未買配件 (高毛利)",
                    "現有品類": existing_str,
                    "缺口品類": "配件",
                    "開場白": OPENING_LINES["rule1"].format(name=name),
                }
            )
        if bought["家電"] and not bought["保健"]:
            rows.append(
                {
                    "客戶名稱": name,
                    "區域": region,
                    "缺口類型": "買家電未買保健 (高毛利)",
                    "現有品類": existing_str,
                    "缺口品類": "保健",
                    "開場白": OPENING_LINES["rule2"].format(name=name),
                }
            )
        if len(existing) == 1:
            rows.append(
                {
                    "客戶名稱": name,
                    "區域": region,
                    "缺口類型": "只買單一品類",
                    "現有品類": existing_str,
                    "缺口品類": "其他 5 品類",
                    "開場白": OPENING_LINES["rule3"].format(name=name, existing=existing[0]),
                }
            )
        if (bought["通訊"] or bought["資訊"]) and not bought["二手機"]:
            rows.append(
                {
                    "客戶名稱": name,
                    "區域": region,
                    "缺口類型": "通訊/資訊 未買二手機",
                    "現有品類": existing_str,
                    "缺口品類": "二手機",
                    "開場白": OPENING_LINES["rule4"].format(name=name),
                }
            )

    return pd.DataFrame(rows)


def main() -> None:
    xlsx = Path("aws-s3/sales/2026/04/績效追蹤4月.xlsx")
    out_path = Path("out/cross_sell_gaps_2026-04.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"📥 讀取 {xlsx} > 同期by經銷商")
    gaps = detect_gaps(xlsx)
    gaps.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"📤 寫出 {out_path} ({len(gaps)} 行)")

    if not gaps.empty:
        summary = gaps.groupby(["區域", "缺口類型"]).size().unstack(fill_value=0)
        print("\n📊 各區域 × 缺口類型 經銷商數:")
        print(summary.to_string())


if __name__ == "__main__":
    main()
