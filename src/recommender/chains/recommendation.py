"""Recommendation chain — LCEL Runnable: dict → RecommendationOutput。

LangChain 最佳實踐:把 chain 當「一等公民」抽成獨立 factory,而非埋在 service 裡。
好處:
  - 可單獨測試 (傳 FakeListChatModel 進來,不打 Bedrock)
  - 可組合 (上游接 RunnableLambda、下游掛 .with_retry() / .with_fallbacks())
  - service 只負責「聚合資料 + 編排 + 寫 DB」,chain 只負責「prompt + 結構化輸出」

chain 不自己建 LLM,改用注入 (build_*_chain(llm)) —— 讓 chain 與「LLM 怎麼來」解耦。
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from recommender.prompts import load_system_prompt
from recommender.schemas.recommendation import RecommendationOutput

RECOMMENDATION_PROMPT_VERSION = "recommendation/v1.0"

# human 觸發語 (含 runtime 注入變數);system 規則本體在 prompts/recommendation/v1.0.md
_HUMAN_TEMPLATE = (
    "請為經銷商 {customer_id} 產出本月推薦報告。\n"
    "參考資料路徑: {dataset_s3_key}\n"
    "POC 階段 dataset 串接尚未完成,先用通用知識產出合理推薦。"
)


def build_recommendation_chain(llm: BaseChatModel) -> Runnable:
    """組推薦 chain。

    輸入: {"customer_id": str, "dataset_s3_key": str}
    輸出: RecommendationOutput (Pydantic,with_structured_output 保證)
    """
    prompt = load_system_prompt(RECOMMENDATION_PROMPT_VERSION, _HUMAN_TEMPLATE)
    return prompt | llm.with_structured_output(RecommendationOutput)
