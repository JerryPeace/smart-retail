"""Chain assembly tests — fake LLM injection, zero Bedrock calls.

Verifies:
  1. build_recommendation_chain: prompt variables satisfied, output is RecommendationOutput
  2. build_judge_chain: prompt variables satisfied, output is {"parsed": EvaluationOutput, "raw": ...}

No DB, no Docker, no network — can be run standalone:
    uv run pytest tests/test_chains.py -v

Design note (⭐ Trade-off 5 from design.md §7.4):
  langchain-core 0.3.x FakeListChatModel / FakeChatModel / GenericFakeChatModel
  all lack bind_tools. with_structured_output() internally calls bind_tools (tool-calling
  mode). Solution: subclass GenericFakeChatModel and stub bind_tools to return
  self.bind(tools=tools, tool_choice=tool_choice), then feed AIMessage with tool_calls
  containing the expected Pydantic output args.
"""
import os

# Must be set before any recommender import
os.environ.setdefault("ANALYZER_MOCK_MODE", "true")

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages.tool import ToolCall

from recommender.chains.judge import JUDGE_PROMPT_VERSION, build_judge_chain
from recommender.chains.recommendation import (
    RECOMMENDATION_PROMPT_VERSION,
    build_recommendation_chain,
)
from recommender.schemas.evaluation import EvaluationOutput
from recommender.schemas.recommendation import RecommendationOutput


# ---------------------------------------------------------------------------
# FakeStructuredChatModel — reusable across both chain tests
# ---------------------------------------------------------------------------

class FakeStructuredChatModel(GenericFakeChatModel):
    """GenericFakeChatModel with stubbed bind_tools.
s
    The messages iterator must contain AIMessage(tool_calls=[...]) whose
    ToolCall.args match the target Pydantic model's fields.
    """

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=tools, tool_choice=tool_choice)


# ---------------------------------------------------------------------------
# Minimal valid output fixtures
# ---------------------------------------------------------------------------

_REC_OUTPUT_ARGS = {
    "schema_version": "v1.0",
    "customer_segment": "high-value",
    "recommended_products": [
        {
            "sku": "SKU001",
            "product_name": "iPhone 15",
            "reason": "Best seller in telecom category for high-value dealers",
            "confidence": 0.90,
        }
    ],
    "customer_insights": {
        "interests": ["通訊", "資訊"],
        "purchase_pattern": "Monthly bulk purchase",
        "next_best_action": "Offer bundle discount",
    },
    "confidence_score": 0.85,
    "generated_at": "2026-06-12T00:00:00Z",
}

_JUDGE_OUTPUT_ARGS = {
    "relevance_score": 0.80,
    "specificity_score": 0.70,
    "actionability_score": 0.75,
    "hallucination_score": 0.90,
    "overall_score": 0.78,
    "judge_reasoning": "The recommendation is well-suited for this dealer type based on historical data.",
    "evaluated_at": "2026-06-12T00:00:00Z",
}

_REC_INPUTS = {
    "customer_id": "TEST_DEALER_001",
    "dataset_s3_key": "s3://cleaned-data/marketing-recommandation/TEST_DEALER_001/2026-05.json",
}

_JUDGE_INPUTS = {
    "customer_id": "TEST_DEALER_001",
    "customer_segment": "high-value",
    "confidence_score": "0.85",
    "products_text": (
        "- [SKU001] iPhone 15 (信心 0.90)\n"
        "  理由: Best seller in telecom category for high-value dealers"
    ),
    "interests": "通訊, 資訊",
    "purchase_pattern": "Monthly bulk purchase",
    "next_best_action": "Offer bundle discount",
}


# ---------------------------------------------------------------------------
# Fixtures — function-scoped so each test gets a fresh chain (iter() is one-shot)
# ---------------------------------------------------------------------------

@pytest.fixture
def rec_chain():
    """Fresh recommendation chain backed by a fake LLM with standard valid output."""
    tc = ToolCall(name="RecommendationOutput", args=_REC_OUTPUT_ARGS, id="call_rec")
    fake_llm = FakeStructuredChatModel(
        messages=iter([AIMessage(content="", tool_calls=[tc])])
    )
    return build_recommendation_chain(fake_llm)


@pytest.fixture
def judge_chain():
    """Fresh judge chain backed by a fake LLM with standard valid output."""
    tc = ToolCall(name="EvaluationOutput", args=_JUDGE_OUTPUT_ARGS, id="call_judge")
    fake_llm = FakeStructuredChatModel(
        messages=iter([AIMessage(content="", tool_calls=[tc])])
    )
    return build_judge_chain(fake_llm)


# ---------------------------------------------------------------------------
# Recommendation chain tests
# ---------------------------------------------------------------------------

class TestBuildRecommendationChain:
    def test_chain_output_is_recommendation_output(self, rec_chain):
        """Chain must parse LLM tool call response into RecommendationOutput."""
        result = rec_chain.invoke(_REC_INPUTS)

        assert isinstance(result, RecommendationOutput)

    def test_output_customer_segment_valid(self, rec_chain):
        result = rec_chain.invoke(_REC_INPUTS)

        assert result.customer_segment in {"high-value", "mid-tier", "new", "churning"}

    def test_output_has_at_least_one_recommended_product(self, rec_chain):
        result = rec_chain.invoke(_REC_INPUTS)

        assert len(result.recommended_products) >= 1

    def test_chain_accepts_required_input_variables(self, rec_chain):
        """Chain should not raise KeyError when given the documented input variables."""
        # Must not raise even with minimal inputs
        result = rec_chain.invoke(_REC_INPUTS)
        assert result is not None

    def test_prompt_version_constant_is_string(self):
        """Smoke-check that the version constant points to an existing .md file."""
        from pathlib import Path
        from recommender.prompts import PROMPTS_ROOT

        prompt_path = PROMPTS_ROOT / f"{RECOMMENDATION_PROMPT_VERSION}.md"
        assert prompt_path.exists(), f"Prompt file missing: {prompt_path}"

    def test_missing_required_input_raises(self, rec_chain):
        """Omitting a required input variable should raise an error from LangChain."""
        with pytest.raises(Exception):
            # customer_id is required — omitting it must raise
            rec_chain.invoke({"dataset_s3_key": "s3://test/key"})


# ---------------------------------------------------------------------------
# Judge chain tests
# ---------------------------------------------------------------------------

class TestBuildJudgeChain:
    def test_chain_output_is_dict_with_parsed_and_raw(self, judge_chain):
        """include_raw=True means chain returns {'parsed': ..., 'raw': ..., ...}."""
        result = judge_chain.invoke(_JUDGE_INPUTS)

        assert isinstance(result, dict)
        assert "parsed" in result
        assert "raw" in result

    def test_parsed_output_is_evaluation_output(self, judge_chain):
        result = judge_chain.invoke(_JUDGE_INPUTS)

        assert isinstance(result["parsed"], EvaluationOutput)

    def test_parsed_scores_in_valid_range(self, judge_chain):
        result = judge_chain.invoke(_JUDGE_INPUTS)

        parsed: EvaluationOutput = result["parsed"]
        for field in ("relevance_score", "specificity_score", "actionability_score",
                      "hallucination_score", "overall_score"):
            value = getattr(parsed, field)
            assert 0.0 <= value <= 1.0, f"{field}={value} out of [0,1]"

    def test_chain_accepts_all_required_input_variables(self, judge_chain):
        """Chain should not raise KeyError for the variables in judge/v1.0.md."""
        result = judge_chain.invoke(_JUDGE_INPUTS)
        assert result is not None

    def test_prompt_version_constant_is_string(self):
        """Smoke-check that the version constant points to an existing .md file."""
        from pathlib import Path
        from recommender.prompts import PROMPTS_ROOT

        prompt_path = PROMPTS_ROOT / f"{JUDGE_PROMPT_VERSION}.md"
        assert prompt_path.exists(), f"Prompt file missing: {prompt_path}"

    def test_raw_message_present_in_result(self, judge_chain):
        """raw field must contain the AIMessage (for token usage extraction)."""
        result = judge_chain.invoke(_JUDGE_INPUTS)

        raw = result["raw"]
        assert raw is not None
        # raw should be an AIMessage-like object (has content attribute)
        assert hasattr(raw, "content")
