"""ETL pure-function unit tests — no DB, no network, no Docker required.

These tests verify the deterministic transformation logic that sits between
raw data and the LLM prompt inputs.  All functions under test are static
methods or instance methods with no IO — they must run in isolation.

Design alignment: "ETL First, LLM Last" — these functions are the safety
net that prevents raw data from reaching the LLM.
"""
import os

# Must be set before any recommender import
os.environ.setdefault("ANALYZER_MOCK_MODE", "true")

import pandas as pd
import pytest

from recommender.schemas.recommendation import (
    CustomerInsight,
    RecommendationOutput,
    RecommendedProduct,
)
from recommender.services.evaluation_service import EvaluationService
from recommender.services.promo_forecast_service import (
    CrossCategoryOpportunity,
    MOEAEvidence,
    PromoForecastService,
    ReasoningChain,
    SHEET_BUSINESS_GROUP_COL,
    SHEET_CATEGORY_COLS,
    SHEET_DATA_START_ROW,
    SHEET_DEALER_ID_COL,
    SHEET_DEALER_NAME_COL,
    SHEET_SALES_REP_COL,
    SHEET_TOTAL_AP_COL,
    ZHUANHU_GROUP_NAME,
)


# ---------------------------------------------------------------------------
# Helpers — shared fixtures (not pytest fixtures, just module-level helpers)
# ---------------------------------------------------------------------------

def _make_df_row(
    group: str,
    dealer_id: str,
    dealer_name: str,
    sales_rep: str,
    total_ap: int,
    cat_values: dict[str, float] | None = None,
) -> list:
    """Build a single row matching the expected sheet column layout."""
    # 15 columns: 0..14
    row = [None] * 15
    row[SHEET_BUSINESS_GROUP_COL] = group
    row[SHEET_DEALER_ID_COL] = dealer_id
    row[SHEET_DEALER_NAME_COL] = dealer_name
    row[SHEET_SALES_REP_COL] = sales_rep
    row[SHEET_TOTAL_AP_COL] = total_ap
    if cat_values:
        for cat, col in SHEET_CATEGORY_COLS.items():
            row[col] = cat_values.get(cat, 0.0)
    return row


def _make_test_df(data_rows: list[list]) -> pd.DataFrame:
    """Prepend the required header rows so iloc[SHEET_DATA_START_ROW:] works."""
    header_rows = [
        [f"h{i}" for i in range(15)]
        for _ in range(SHEET_DATA_START_ROW)
    ]
    return pd.DataFrame(header_rows + data_rows)


def _make_reasoning(confidence="HIGH") -> ReasoningChain:
    return ReasoningChain(
        signal="test signal",
        logic="test logic",
        assumption="test assumption",
        confidence=confidence,
        expected="30-100萬/月",
        risk="some risk",
    )


def _make_opportunity(
    dealer_id="000101",
    dealer_name="商號甲",
    priority="P1",
    monthly_total_ap=5_000_000,
    target_category="保健",
) -> CrossCategoryOpportunity:
    return CrossCategoryOpportunity(
        dealer_id=dealer_id,
        dealer_name=dealer_name,
        sales_rep="業務A",
        target_category=target_category,
        priority=priority,
        reasoning=_make_reasoning(),
        moea_evidence=[],
        monthly_total_ap=monthly_total_ap,
    )


# ---------------------------------------------------------------------------
# PromoForecastService._filter_zhuanhu_dealers
# ---------------------------------------------------------------------------

class TestFilterZhuanhuDealers:
    def test_keeps_zhuanhu_with_positive_ap(self):
        df = _make_test_df([
            _make_df_row(ZHUANHU_GROUP_NAME, "000101", "商號甲", "業務A", 5_000_000),
        ])
        result = PromoForecastService._filter_zhuanhu_dealers(df)
        assert len(result) == 1
        assert result.iloc[0][SHEET_DEALER_ID_COL] == "000101"

    def test_excludes_non_zhuanhu_group(self):
        df = _make_test_df([
            _make_df_row("其他業務課", "999999", "其他商", "業務X", 3_000_000),
        ])
        result = PromoForecastService._filter_zhuanhu_dealers(df)
        assert len(result) == 0

    def test_excludes_zero_ap(self):
        df = _make_test_df([
            _make_df_row(ZHUANHU_GROUP_NAME, "000101", "商號甲", "業務A", 0),
        ])
        result = PromoForecastService._filter_zhuanhu_dealers(df)
        assert len(result) == 0

    def test_mixed_rows(self):
        df = _make_test_df([
            _make_df_row(ZHUANHU_GROUP_NAME, "000101", "商號甲", "業務A", 5_000_000),
            _make_df_row("其他業務課", "999999", "其他", "業務X", 2_000_000),
            _make_df_row(ZHUANHU_GROUP_NAME, "9000001", "商號乙", "業務C", 0),
            _make_df_row(ZHUANHU_GROUP_NAME, "000102", "商號丙", "業務D", 3_000_000),
        ])
        result = PromoForecastService._filter_zhuanhu_dealers(df)
        dealer_ids = result[SHEET_DEALER_ID_COL].tolist()
        assert "000101" in dealer_ids
        assert "000102" in dealer_ids
        assert "999999" not in dealer_ids   # wrong group
        assert "9000001" not in dealer_ids  # zero AP

    def test_result_index_reset(self):
        df = _make_test_df([
            _make_df_row(ZHUANHU_GROUP_NAME, "000101", "商號甲", "業務A", 5_000_000),
            _make_df_row(ZHUANHU_GROUP_NAME, "000102", "商號丙", "業務D", 3_000_000),
        ])
        result = PromoForecastService._filter_zhuanhu_dealers(df)
        assert list(result.index) == [0, 1]


# ---------------------------------------------------------------------------
# PromoForecastService._normalize_dealer_id
# ---------------------------------------------------------------------------

class TestNormalizeDealerId:
    @pytest.mark.parametrize("raw,expected", [
        ("000101", "000101"),       # already 6-digit string
        ("9000001", "9000001"),     # 7-digit stays as-is
        (101, "000101"),            # int < 1000000 → zero-padded to 6 digits
        (12345, "012345"),          # int < 1000000 → zero-padded
        (9000001, "9000001"),       # int >= 1000000 → str no padding
        ("abc", "abc"),             # non-numeric string passes through
        (" 000101 ", "000101"),     # whitespace stripped (exception path → str.strip)
    ])
    def test_normalize(self, raw, expected):
        assert PromoForecastService._normalize_dealer_id(raw) == expected


# ---------------------------------------------------------------------------
# PromoForecastService._classify_legal_categories
# ---------------------------------------------------------------------------

class TestClassifyLegalCategories:
    def test_known_codes_map_correctly(self):
        scope = [
            ("F213060", "電信服務"),   # 通訊
            ("F213030", "資訊設備"),   # 資訊
            ("F102170", "健康食品"),   # 保健
        ]
        result = PromoForecastService._classify_legal_categories(scope)
        assert result == {"通訊", "資訊", "保健"}

    def test_unknown_codes_ignored(self):
        scope = [
            ("XXXXXX", "未知事業"),
            ("F213060", "電信服務"),
        ]
        result = PromoForecastService._classify_legal_categories(scope)
        assert result == {"通訊"}

    def test_empty_scope(self):
        result = PromoForecastService._classify_legal_categories([])
        assert result == set()

    def test_duplicate_category_codes(self):
        # Two different codes both map to 通訊 (telecom)
        scope = [
            ("F213060", "電信服務"),
            ("F113070", "行動通訊"),
        ]
        result = PromoForecastService._classify_legal_categories(scope)
        assert result == {"通訊"}


# ---------------------------------------------------------------------------
# PromoForecastService._build_reasoning
# ---------------------------------------------------------------------------

class TestBuildReasoning:
    def test_returns_reasoning_chain(self):
        reasoning = PromoForecastService._build_reasoning(
            dealer_name="商號甲",
            target_cat="保健",
            moea_scope=[("F102170", "健康食品"), ("F203010", "藥品批發")],
            actual={"通訊": 5_000_000.0, "保健": 0.0},
        )
        assert isinstance(reasoning, ReasoningChain)

    def test_high_confidence_requires_evidence_and_sales(self):
        reasoning = PromoForecastService._build_reasoning(
            dealer_name="商號甲",
            target_cat="保健",
            moea_scope=[("F102170", "健康食品")],
            actual={"通訊": 5_000_000.0, "保健": 0.0},
        )
        assert reasoning.confidence == "HIGH"

    def test_medium_confidence_evidence_only(self):
        """Has MOEA evidence but no existing sales (no main category)."""
        reasoning = PromoForecastService._build_reasoning(
            dealer_name="商號甲",
            target_cat="保健",
            moea_scope=[("F102170", "健康食品")],
            actual={"通訊": 0.0, "保健": 0.0},  # all zero
        )
        assert reasoning.confidence == "MEDIUM"

    def test_medium_confidence_sales_only(self):
        """Has sales history but no MOEA evidence for target category."""
        reasoning = PromoForecastService._build_reasoning(
            dealer_name="商號甲",
            target_cat="保健",
            moea_scope=[],  # no evidence
            actual={"通訊": 5_000_000.0, "保健": 0.0},
        )
        assert reasoning.confidence == "MEDIUM"

    def test_low_confidence_no_evidence_no_sales(self):
        reasoning = PromoForecastService._build_reasoning(
            dealer_name="商號甲",
            target_cat="保健",
            moea_scope=[],
            actual={"通訊": 0.0, "保健": 0.0},
        )
        assert reasoning.confidence == "LOW"

    def test_signal_contains_target_category(self):
        reasoning = PromoForecastService._build_reasoning(
            dealer_name="商號乙",
            target_cat="資訊",
            moea_scope=[("F213030", "資訊設備")],
            actual={"通訊": 1_000_000.0, "資訊": 0.0},
        )
        assert "資訊" in reasoning.signal

    def test_reasoning_fields_all_present(self):
        reasoning = PromoForecastService._build_reasoning(
            dealer_name="商號甲",
            target_cat="家電",
            moea_scope=[("F213010", "家用電器")],
            actual={"通訊": 2_000_000.0, "家電": 0.0},
        )
        assert len(reasoning.signal) > 0
        assert len(reasoning.logic) > 0
        assert len(reasoning.assumption) > 0
        assert len(reasoning.expected) > 0
        assert len(reasoning.risk) > 0


# ---------------------------------------------------------------------------
# PromoForecastService._rank_opportunities
# ---------------------------------------------------------------------------

class TestRankOpportunities:
    def test_p1_before_p2_before_p3(self):
        opps = [
            _make_opportunity(dealer_id="A", priority="P3", monthly_total_ap=9_000_000),
            _make_opportunity(dealer_id="B", priority="P1", monthly_total_ap=1_000_000),
            _make_opportunity(dealer_id="C", priority="P2", monthly_total_ap=5_000_000),
        ]
        ranked = PromoForecastService._rank_opportunities(opps)
        priorities = [o.priority for o in ranked]
        assert priorities == ["P1", "P2", "P3"]

    def test_same_priority_higher_ap_first(self):
        opps = [
            _make_opportunity(dealer_id="A", priority="P1", monthly_total_ap=3_000_000),
            _make_opportunity(dealer_id="B", priority="P1", monthly_total_ap=8_000_000),
            _make_opportunity(dealer_id="C", priority="P1", monthly_total_ap=5_000_000),
        ]
        ranked = PromoForecastService._rank_opportunities(opps)
        aps = [o.monthly_total_ap for o in ranked]
        assert aps == [8_000_000, 5_000_000, 3_000_000]

    def test_empty_list(self):
        assert PromoForecastService._rank_opportunities([]) == []

    def test_original_list_not_mutated(self):
        opps = [
            _make_opportunity(dealer_id="A", priority="P2", monthly_total_ap=1_000_000),
            _make_opportunity(dealer_id="B", priority="P1", monthly_total_ap=2_000_000),
        ]
        original_first = opps[0].dealer_id
        PromoForecastService._rank_opportunities(opps)
        assert opps[0].dealer_id == original_first  # sorted() returns new list, input unchanged


# ---------------------------------------------------------------------------
# PromoForecastService.opportunities_to_dataframe
# ---------------------------------------------------------------------------

class TestOpportunitiesToDataframe:
    def test_returns_dataframe(self):
        opps = [_make_opportunity()]
        df = PromoForecastService.opportunities_to_dataframe(opps)
        assert isinstance(df, pd.DataFrame)

    def test_expected_columns_present(self):
        opps = [_make_opportunity()]
        df = PromoForecastService.opportunities_to_dataframe(opps)
        expected_cols = ["客代", "客戶名稱", "業務員", "上月業績", "推薦品類", "優先級", "信心度"]
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_row_values_match_opportunity(self):
        opp = CrossCategoryOpportunity(
            dealer_id="000101",
            dealer_name="商號甲",
            sales_rep="業務甲",
            target_category="保健",
            priority="P1",
            reasoning=_make_reasoning("HIGH"),
            moea_evidence=[MOEAEvidence(code="F102170", description="健康食品")],
            monthly_total_ap=5_000_000,
        )
        df = PromoForecastService.opportunities_to_dataframe([opp])
        row = df.iloc[0]
        assert row["客代"] == "000101"
        assert row["客戶名稱"] == "商號甲"
        assert row["推薦品類"] == "保健"
        assert row["優先級"] == "P1"
        assert row["信心度"] == "HIGH"
        assert row["上月業績"] == 5_000_000

    def test_moea_evidence_joined(self):
        opp = CrossCategoryOpportunity(
            dealer_id="000101",
            dealer_name="商號甲",
            sales_rep="業務甲",
            target_category="保健",
            priority="P1",
            reasoning=_make_reasoning(),
            moea_evidence=[
                MOEAEvidence(code="F102170", description="健康食品"),
                MOEAEvidence(code="F203010", description="藥品批發"),
            ],
            monthly_total_ap=5_000_000,
        )
        df = PromoForecastService.opportunities_to_dataframe([opp])
        evidence_str = df.iloc[0]["經濟部證據"]
        assert "健康食品" in evidence_str
        assert "藥品批發" in evidence_str

    def test_empty_list_returns_empty_dataframe(self):
        df = PromoForecastService.opportunities_to_dataframe([])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0


# ---------------------------------------------------------------------------
# EvaluationService._build_inputs
# ---------------------------------------------------------------------------

def _make_eval_service() -> EvaluationService:
    """Create a minimal EvaluationService without DB connections."""

    class _DummyRepo:
        pass

    svc = EvaluationService.__new__(EvaluationService)
    svc.rec_repo = _DummyRepo()
    svc.eval_repo = _DummyRepo()
    svc.mock_mode = True
    return svc


def _make_recommendation_output(
    customer_segment="high-value",
    products: list[dict] | None = None,
    interests: list[str] | None = None,
    confidence_score: float = 0.85,
) -> RecommendationOutput:
    products = products or [
        {"sku": "SKU001", "product_name": "iPhone 15", "reason": "Top seller for telecom dealers in this segment", "confidence": 0.90},
        {"sku": "SKU002", "product_name": "MacBook Air", "reason": "Consistent demand from enterprise customers", "confidence": 0.75},
    ]
    return RecommendationOutput(
        customer_segment=customer_segment,
        recommended_products=[RecommendedProduct(**p) for p in products],
        customer_insights=CustomerInsight(
            interests=interests or ["通訊", "資訊"],
            purchase_pattern="Monthly bulk orders",
            next_best_action="Offer bundle discount",
        ),
        confidence_score=confidence_score,
    )


class TestBuildInputs:
    def test_returns_all_required_keys(self):
        svc = _make_eval_service()
        output = _make_recommendation_output()
        inputs = svc._build_inputs("TEST_DEALER_001", output)

        required_keys = {
            "customer_id",
            "customer_segment",
            "confidence_score",
            "products_text",
            "interests",
            "purchase_pattern",
            "next_best_action",
        }
        assert set(inputs.keys()) == required_keys

    def test_customer_id_passthrough(self):
        svc = _make_eval_service()
        output = _make_recommendation_output()
        inputs = svc._build_inputs("DEALER_XYZ", output)
        assert inputs["customer_id"] == "DEALER_XYZ"

    def test_confidence_score_formatted_as_two_decimals(self):
        svc = _make_eval_service()
        output = _make_recommendation_output(confidence_score=0.853)
        inputs = svc._build_inputs("D001", output)
        # Format is "{score:.2f}"
        assert inputs["confidence_score"] == "0.85"

    def test_products_text_format(self):
        """Each product must have a bullet line + reason line matching the expected format."""
        svc = _make_eval_service()
        output = _make_recommendation_output(
            products=[
                {"sku": "SKU001", "product_name": "iPhone 15", "reason": "Top seller for telecom dealers in this region", "confidence": 0.90},
            ]
        )
        inputs = svc._build_inputs("D001", output)
        text = inputs["products_text"]
        # Bullet line: "- [SKU001] iPhone 15 (信心 0.90)"
        assert "- [SKU001] iPhone 15 (信心 0.90)" in text
        # Reason line: "  理由: ..."
        assert "  理由: Top seller for telecom dealers in this region" in text

    def test_products_text_aggregates_multiple_products(self):
        svc = _make_eval_service()
        output = _make_recommendation_output(
            products=[
                {"sku": "SKU001", "product_name": "Prod A", "reason": "Reason for product A being good for dealers", "confidence": 0.90},
                {"sku": "SKU002", "product_name": "Prod B", "reason": "Reason for product B suiting this dealer segment", "confidence": 0.75},
            ]
        )
        inputs = svc._build_inputs("D001", output)
        text = inputs["products_text"]
        # Each product generates two lines separated by newline
        lines = text.split("\n")
        assert len(lines) == 4  # 2 products × 2 lines each

    def test_interests_joined_with_comma(self):
        svc = _make_eval_service()
        output = _make_recommendation_output(interests=["通訊", "資訊", "家電"])
        inputs = svc._build_inputs("D001", output)
        assert inputs["interests"] == "通訊, 資訊, 家電"

    def test_single_interest(self):
        svc = _make_eval_service()
        output = _make_recommendation_output(interests=["通訊"])
        inputs = svc._build_inputs("D001", output)
        assert inputs["interests"] == "通訊"

    def test_customer_segment_passthrough(self):
        svc = _make_eval_service()
        output = _make_recommendation_output(customer_segment="churning")
        inputs = svc._build_inputs("D001", output)
        assert inputs["customer_segment"] == "churning"
