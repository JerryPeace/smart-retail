"""Recommendation chain — LCEL Runnable: dict → RecommendationOutput.

LangChain best practice: treat the chain as a "first-class citizen" extracted into a standalone factory, rather than buried inside the service.
Benefits:
  - Can be tested in isolation (pass in a FakeListChatModel, no Bedrock calls)
  - Composable (attach a RunnableLambda upstream, hang .with_retry() / .with_fallbacks() downstream)
  - the service only handles "aggregate data + orchestrate + write DB", the chain only handles "prompt + structured output"

The chain doesn't build the LLM itself; it's injected (build_*_chain(llm)) — decoupling the chain from "where the LLM comes from".
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from recommender.prompts import load_system_prompt
from recommender.schemas.recommendation import RecommendationOutput

RECOMMENDATION_PROMPT_VERSION = "recommendation/v1.0"

# human trigger phrase (contains runtime-injected variables); the system rules body is in prompts/recommendation/v1.0.md
# {month} ("YYYY-MM") grounds the seasonal/peak-season reasoning defined in the system prompt.
_HUMAN_TEMPLATE = (
    "請為經銷商 {customer_id} 產出 {month} 的推薦報告。\n"
    "參考資料路徑: {dataset_s3_key}\n"
    "POC 階段 dataset 串接尚未完成,先用通用知識 + 系統 prompt 的品類旺季表產出合理推薦。"
)


def build_recommendation_chain(llm: BaseChatModel) -> Runnable:
    """Assemble the recommendation chain.

    Input:  {"customer_id": str, "month": str, "dataset_s3_key": str}
    Output: RecommendationOutput (Pydantic, guaranteed by with_structured_output)
    """
    prompt = load_system_prompt(RECOMMENDATION_PROMPT_VERSION, _HUMAN_TEMPLATE)
    return prompt | llm.with_structured_output(RecommendationOutput)
