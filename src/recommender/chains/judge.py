"""Judge chain — LCEL Runnable: dict → {"parsed": EvaluationOutput, "raw": ...}。

LLM-as-judge 的 chain。include_raw=True 讓輸出帶 raw message,service 從中抽 token usage。
資料聚合 (products_text 等) 留在 service 的 _build_inputs (ETL First, LLM Last),
chain 只收已聚合好的 dict。
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from recommender.prompts import load_system_prompt
from recommender.schemas.evaluation import EvaluationOutput

JUDGE_PROMPT_VERSION = "judge/v1.0"

_HUMAN_TEMPLATE = "請依上述五個維度為這份推薦評分,輸出 EvaluationOutput。"


def build_judge_chain(llm: BaseChatModel) -> Runnable:
    """組 judge chain。

    輸入: judge/v1.0.md 需要的注入變數 dict (customer_id / products_text / ...)
    輸出: dict {"parsed": EvaluationOutput, "raw": AIMessage}（include_raw=True）
    """
    prompt = load_system_prompt(JUDGE_PROMPT_VERSION, _HUMAN_TEMPLATE)
    return prompt | llm.with_structured_output(EvaluationOutput, include_raw=True)
