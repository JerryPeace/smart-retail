"""Judge chain — LCEL Runnable: dict → {"parsed": EvaluationOutput, "raw": ...}.

The LLM-as-judge chain. include_raw=True makes the output carry the raw message, from which the service extracts token usage.
Data aggregation (products_text, etc.) stays in the service's _build_inputs (ETL First, LLM Last);
the chain only receives an already-aggregated dict.
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from recommender.prompts import load_system_prompt
from recommender.schemas.evaluation import EvaluationOutput

JUDGE_PROMPT_VERSION = "judge/v1.0"

_HUMAN_TEMPLATE = "請依上述五個維度為這份推薦評分,輸出 EvaluationOutput。"


def build_judge_chain(llm: BaseChatModel) -> Runnable:
    """Assemble the judge chain.

    Input:  the dict of injected variables that judge/v1.0.md needs (customer_id / products_text / ...)
    Output: dict {"parsed": EvaluationOutput, "raw": AIMessage} (include_raw=True)
    """
    prompt = load_system_prompt(JUDGE_PROMPT_VERSION, _HUMAN_TEMPLATE)
    return prompt | llm.with_structured_output(EvaluationOutput, include_raw=True)
