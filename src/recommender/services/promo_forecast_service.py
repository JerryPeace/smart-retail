"""PromoForecastService — monthly key-account promo forecast (R8 upgraded version).

POC scope:
  - Scope: the 33 active dealers of the key-account sales team
  - R8 cross-category opportunity: legally sellable (Ministry of Economic Affairs) + 0 purchases in that category last month + strategic alignment
  - No LLM calls (dry_run mode), pure deterministic ETL + reasoning chain

Design principles (aligned with docs/plans/promo-forecast-data-fitness.md):
  - No ML — deterministic rules + LLM narrative
  - Negative constraints: exclude used phones (reverse business), tablets (wind down)
  - The reasoning chain is a hard requirement (signal/logic/assumption/confidence/expected/risk)

Data sources:
  - Monthly fact: the new file `104e 客戶別.xlsx` > `{N}月` sheet
  - HubSpot tax-id cache: external JSON data/zhuanhu_tax_ids.json (gitignored, contains PII; production should connect to HubSpotService)
  - Registered business scope: Ministry of Economic Affairs public records (via the g0v Company Bao API)
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import cache
from io import BytesIO
from pathlib import Path
from typing import Literal

import pandas as pd
import requests
from pydantic import BaseModel

from recommender.config import settings
from recommender.services.s3_service import S3Service

logger = logging.getLogger(__name__)


# ============================================================================
# Module constants
# ============================================================================

# The 5 recommendable categories (excludes used phones / tablets being wound down)
PROMO_CATEGORIES = ["通訊", "資訊", "家電", "配件", "保健"]

# Industry code → company category mapping v1 (24 entries)
# Expansion list pending PO sign-off, see docs/plans/promo-forecast-moea-business-scope.md §3.2
INDUSTRY_CODE_MAP: dict[str, str] = {
    # Communications group
    "F213060": "通訊", "F113070": "通訊", "IE01010": "通訊",
    "CC01060": "通訊", "CC01070": "通訊",
    # IT group
    "F213030": "資訊", "F113050": "資訊", "F118010": "資訊",
    "F119010": "資訊", "I301010": "資訊", "I301030": "資訊",
    "E605010": "資訊",
    # Home appliance group
    "F213010": "家電", "F113020": "家電", "E601020": "家電",
    # Accessories group
    "F209060": "配件", "F109070": "配件", "F206020": "配件",
    "F116010": "配件",
    # Health group (strategic category)
    "F102170": "保健", "F203010": "保健", "F108031": "保健",
    "F208031": "保健", "F208040": "保健", "F208050": "保健",
}

# Strategic push priority (corresponds to promo-forecast-moea-business-scope.md §4)
STRATEGY_PUSH: dict[str, dict] = {
    "保健": {
        "priority": "P1",
        "growth_target": 7.0,
        "reason": "公司 2026 押 7× 成長 (708 萬 vs 2025 101 萬)",
    },
    "資訊": {
        "priority": "P2",
        "growth_target": 1.51,
        "reason": "公司 2026 目標 +51% (2.32 億 vs 2025 1.53 億)",
    },
    "家電": {
        "priority": "P3",
        "growth_target": 1.20,
        "reason": "毛利率 8% 最高 (vs 通訊 2%)",
    },
}

# Tax IDs of the 33 active dealers of the key-account sales team (HubSpot 2026-05-13 snapshot)
# Production: should query s_cust_bt_taxidnumber dynamically from the HubSpot client
# Key-account dealer code → tax-id mapping (PII). The contents live in an external JSON,
# gitignored, and do not go into the public repo.
# See data/zhuanhu_tax_ids.example.json for the structure; production should switch to HubSpotService.
_TAX_IDS_PATH = Path(__file__).resolve().parents[3] / "data" / "zhuanhu_tax_ids.json"


@cache
def load_zhuanhu_tax_ids() -> dict[str, str]:
    """Load the dealer-code → tax-id mapping.

    If the file doesn't exist (e.g. a public repo clone, or data not yet placed) → return {}
    and warn, so cross-category forecasting yields an empty result rather than crashing
    (aligned with the MOEA fallback strategy of "not found is treated as no data").
    """
    if not _TAX_IDS_PATH.exists():
        logger.warning(
            "找不到 %s,專戶統編表為空 — 跨品類預測將無對象。"
            "請放置該檔 (見 data/zhuanhu_tax_ids.example.json) 或改接 HubSpotService。",
            _TAX_IDS_PATH,
        )
        return {}
    return json.loads(_TAX_IDS_PATH.read_text(encoding="utf-8"))

# Monthly sheet structure (aligned with the new file 104e 客戶別 sheets for months 4-11)
SHEET_BUSINESS_GROUP_COL = 0  # "key-account sales team"
SHEET_DEALER_ID_COL = 1
SHEET_DEALER_NAME_COL = 2
SHEET_SALES_REP_COL = 4
SHEET_DATA_START_ROW = 4
SHEET_CATEGORY_COLS = {
    "通訊": 6, "平板": 7, "資訊": 8, "家電": 9,
    "周邊": 10, "保健": 11, "二手機": 12,
}
SHEET_TOTAL_AP_COL = 14
ZHUANHU_GROUP_NAME = "專戶業務課"

# Ministry of Economic Affairs API (g0v Company Bao wrapper, backed by the MOEA business-registration public records)
MOEA_API_BASE = "https://company.g0v.ronny.tw/api/show"
MOEA_REQUEST_TIMEOUT = 15
MOEA_RATE_LIMIT_DELAY = 0.3


# ============================================================================
# Pydantic Schemas (inlined, matching the SalesAnalysisService inline style)
# ============================================================================

class ReasoningChain(BaseModel):
    signal: str
    logic: str
    assumption: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    expected: str
    risk: str


class MOEAEvidence(BaseModel):
    code: str
    description: str


class CrossCategoryOpportunity(BaseModel):
    dealer_id: str
    dealer_name: str
    sales_rep: str
    target_category: str
    priority: Literal["P1", "P2", "P3"]
    reasoning: ReasoningChain
    moea_evidence: list[MOEAEvidence]
    monthly_total_ap: int


# ============================================================================
# Service
# ============================================================================


class PromoForecastService:
    """Monthly key-account promo forecast (R8 cross-category opportunity, POC v1)."""

    def __init__(self, s3: S3Service | None = None) -> None:
        self.s3 = s3

    # ====================================================================
    # Public interface
    # ====================================================================

    async def run_from_dataframe(
        self,
        month: str,
        df_monthly: pd.DataFrame,
    ) -> dict:
        """Run one pass from an already-loaded DataFrame (POC dry-run friendly).

        Args:
            month: "YYYY-MM" e.g. "2026-04"
            df_monthly: the full monthly sheet DataFrame (including header rows)
        """
        df_zhuanhu = self._filter_zhuanhu_dealers(df_monthly)
        moea_data = await self._batch_query_moea(list(load_zhuanhu_tax_ids().values()))
        opps = self._detect_cross_category_opportunities(df_zhuanhu, moea_data)
        opps_sorted = self._rank_opportunities(opps)
        return self._build_summary(month, df_zhuanhu, opps_sorted)

    async def run_from_local_xlsx(self, month: str, xlsx_path: Path) -> dict:
        """POC: run from a local xlsx (no S3 / HubSpot client needed)."""
        year, mm = month.split("-")
        sheet_name = f"{int(mm)}月"
        df = await asyncio.to_thread(
            pd.read_excel, xlsx_path, sheet_name=sheet_name,
            header=None, engine="openpyxl",
        )
        return await self.run_from_dataframe(month, df)

    # ====================================================================
    # Internal: ETL
    # ====================================================================

    @staticmethod
    def _filter_zhuanhu_dealers(df: pd.DataFrame) -> pd.DataFrame:
        """Filter the key-account sales team's active dealers for the month (~33)."""
        data = df.iloc[SHEET_DATA_START_ROW:].reset_index(drop=True)
        mask_group = data[SHEET_BUSINESS_GROUP_COL] == ZHUANHU_GROUP_NAME
        zhuanhu = data[mask_group].copy()
        total_ap = pd.to_numeric(zhuanhu[SHEET_TOTAL_AP_COL], errors="coerce")
        zhuanhu = zhuanhu[total_ap > 0]
        return zhuanhu.reset_index(drop=True)

    @staticmethod
    def _normalize_dealer_id(raw) -> str:
        try:
            n = int(raw)
            return f"{n:06d}" if n < 1000000 else str(n)
        except Exception:
            return str(raw).strip()

    @staticmethod
    def _classify_legal_categories(scope: list[tuple[str, str]]) -> set[str]:
        """Derive the set of company categories that are legally sellable, from the registered business-scope codes."""
        return {
            INDUSTRY_CODE_MAP[code]
            for code, _desc in scope
            if code in INDUSTRY_CODE_MAP
        }

    def _detect_cross_category_opportunities(
        self,
        df: pd.DataFrame,
        moea_data: dict[str, list[tuple[str, str]]],
    ) -> list[CrossCategoryOpportunity]:
        """R8 upgraded version: legally sellable + 0 purchases last month + strategic alignment."""
        opportunities: list[CrossCategoryOpportunity] = []
        for _, row in df.iterrows():
            dealer_id = self._normalize_dealer_id(row[SHEET_DEALER_ID_COL])
            tax_id = load_zhuanhu_tax_ids().get(dealer_id)
            if not tax_id:
                continue

            dealer_name = str(row[SHEET_DEALER_NAME_COL]).strip()
            sales_rep = str(row[SHEET_SALES_REP_COL]).strip() if pd.notna(row[SHEET_SALES_REP_COL]) else ""
            scope = moea_data.get(tax_id, [])
            legal = self._classify_legal_categories(scope)

            actual = {
                cat: float(row[col]) if pd.notna(row[col]) else 0.0
                for cat, col in SHEET_CATEGORY_COLS.items()
            }
            total_ap = int(row[SHEET_TOTAL_AP_COL]) if pd.notna(row[SHEET_TOTAL_AP_COL]) else 0

            for target in PROMO_CATEGORIES:
                if target not in legal:
                    continue  # no legal registration
                if actual.get(target, 0) > 0:
                    continue  # already purchased, not a cross-sell
                if target not in STRATEGY_PUSH:
                    continue  # no strategic alignment
                evidence = [
                    MOEAEvidence(code=c, description=d)
                    for c, d in scope
                    if INDUSTRY_CODE_MAP.get(c) == target
                ]
                reasoning = self._build_reasoning(
                    dealer_name=dealer_name,
                    target_cat=target,
                    moea_scope=scope,
                    actual=actual,
                )
                opportunities.append(CrossCategoryOpportunity(
                    dealer_id=dealer_id,
                    dealer_name=dealer_name,
                    sales_rep=sales_rep,
                    target_category=target,
                    priority=STRATEGY_PUSH[target]["priority"],
                    reasoning=reasoning,
                    moea_evidence=evidence,
                    monthly_total_ap=total_ap,
                ))
        return opportunities

    @staticmethod
    def _build_reasoning(
        *,
        dealer_name: str,
        target_cat: str,
        moea_scope: list[tuple[str, str]],
        actual: dict[str, float],
    ) -> ReasoningChain:
        evidence = [
            d for c, d in moea_scope
            if INDUSTRY_CODE_MAP.get(c) == target_cat
        ]
        evidence_str = " / ".join(evidence[:3]) if evidence else "(無)"
        strategy = STRATEGY_PUSH[target_cat]

        sales_categories = {k: v for k, v in actual.items() if v > 0}
        main_cat = max(sales_categories, key=sales_categories.get) if sales_categories else "(無)"
        main_amount = int(sales_categories.get(main_cat, 0))

        # review #5: confidence is derived from "evidence quality" rather than always being HIGH.
        # Signals: whether the legal registration directly hits this category (evidence) + whether there's a healthy core business to carry it.
        # The thresholds are heuristic, pending PO calibration (see docs/plans/promo-forecast-moea-business-scope.md).
        if evidence and sales_categories:
            confidence: Literal["HIGH", "MEDIUM", "LOW"] = "HIGH"
        elif evidence or sales_categories:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        return ReasoningChain(
            signal=(
                f"經濟部所營事業含 {target_cat} 相關登記: {evidence_str}; "
                f"上月 {target_cat} 實際採購 0 元; "
                f"主業為 {main_cat} ({main_amount:,} 元); "
                f"公司戰略: {strategy['reason']}"
            ),
            logic=(
                f"法定可賣 ({target_cat} 在公司登記中) + 戰略 push ({strategy['priority']}) + "
                f"主業 {main_cat} 健康 → {target_cat} 試水溫有承載條件"
            ),
            assumption=(
                f"{dealer_name} 客群結構能承載 {target_cat} 銷售; "
                f"本公司 端有對應 SKU 供應"
            ),
            confidence=confidence,
            expected=(
                f"試水溫 2-3 SKU, 預估月銷 30-100 萬, 持續 3 個月評估"
            ),
            risk=(
                f"基於 implicit 訊號 (法定登記), 非該專戶主動表達需求, 首單可能慢"
            ),
        )

    @staticmethod
    def _rank_opportunities(
        opps: list[CrossCategoryOpportunity],
    ) -> list[CrossCategoryOpportunity]:
        priority_order = {"P1": 0, "P2": 1, "P3": 2}
        return sorted(
            opps,
            key=lambda o: (priority_order[o.priority], -o.monthly_total_ap),
        )

    # ====================================================================
    # Internal: Ministry of Economic Affairs API
    # ====================================================================

    async def _batch_query_moea(
        self,
        tax_ids: list[str],
    ) -> dict[str, list[tuple[str, str]]]:
        result: dict[str, list[tuple[str, str]]] = {}
        for tax_id in tax_ids:
            scope = await asyncio.to_thread(self._query_single_moea, tax_id)
            result[tax_id] = scope
            await asyncio.sleep(MOEA_RATE_LIMIT_DELAY)
        return result

    @staticmethod
    def _query_single_moea(tax_id: str) -> list[tuple[str, str]]:
        # review #5: when the business scope isn't found, return [] (downstream treats it as
        # "no legal category evidence"), but it must not be silent — use log.warning to leave a
        # trace, making it possible to later distinguish "genuinely not registered"
        # vs "the API went down / was rate-limited".
        try:
            r = requests.get(
                f"{MOEA_API_BASE}/{tax_id}",
                timeout=MOEA_REQUEST_TIMEOUT,
            )
            if r.status_code != 200:
                logger.warning("MOEA query %s 回非 200 (%s),視為無資料", tax_id, r.status_code)
                return []
            payload = r.json()
            data = payload.get("data") or payload
            biz = data.get("所營事業資料") or []
            return [
                (b[0], b[1])
                for b in biz
                if isinstance(b, list) and len(b) >= 2
            ]
        except Exception:
            logger.warning("MOEA query %s 例外,視為無資料", tax_id, exc_info=True)
            return []

    # ====================================================================
    # Internal: statistical output
    # ====================================================================

    @staticmethod
    def _build_summary(
        month: str,
        df_zhuanhu: pd.DataFrame,
        opps: list[CrossCategoryOpportunity],
    ) -> dict:
        return {
            "month": month,
            "dealer_count": len(df_zhuanhu),
            "opportunities_count": len(opps),
            "by_priority": {
                p: sum(1 for o in opps if o.priority == p)
                for p in ["P1", "P2", "P3"]
            },
            "by_category": {
                cat: sum(1 for o in opps if o.target_category == cat)
                for cat in PROMO_CATEGORIES
            },
            "opportunities": [o.model_dump() for o in opps],
        }

    # ====================================================================
    # Public: CSV output (for future S3 writes)
    # ====================================================================

    @staticmethod
    def opportunities_to_dataframe(
        opps: list[CrossCategoryOpportunity],
    ) -> pd.DataFrame:
        rows = []
        for o in opps:
            rows.append({
                "客代": o.dealer_id,
                "客戶名稱": o.dealer_name,
                "業務員": o.sales_rep,
                "上月業績": o.monthly_total_ap,
                "推薦品類": o.target_category,
                "優先級": o.priority,
                "信心度": o.reasoning.confidence,
                "經濟部證據": " / ".join(
                    e.description for e in o.moea_evidence[:3]
                ),
                "推理_signal": o.reasoning.signal,
                "推理_logic": o.reasoning.logic,
                "前提": o.reasoning.assumption,
                "預期效果": o.reasoning.expected,
                "風險": o.reasoning.risk,
            })
        return pd.DataFrame(rows)
