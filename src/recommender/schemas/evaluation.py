"""Evaluation schemas — LLM-as-judge 對 recommendation 的評分"""
from datetime import datetime

from recommender.timeutil import utcnow

from pydantic import BaseModel, Field


class EvaluationOutput(BaseModel):
    """Judge LLM 結構化輸出 (5 維,0-1 連續分數)"""

    relevance_score: float = Field(..., ge=0, le=1, description="推薦商品是否適合此經銷商")
    specificity_score: float = Field(..., ge=0, le=1, description="理由是否引用具體證據")
    actionability_score: float = Field(..., ge=0, le=1, description="業務拿著能否直接行動")
    hallucination_score: float = Field(..., ge=0, le=1, description="事實宣稱可信度,高=沒編造")
    overall_score: float = Field(..., ge=0, le=1, description="整體商業價值與可信度,judge 獨立判斷")

    judge_reasoning: str = Field(..., min_length=10, max_length=1000)
    evaluated_at: datetime = Field(default_factory=utcnow)
