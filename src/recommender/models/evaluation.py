"""Evaluation — LLM-as-judge 對 Recommendation 的評分紀錄"""
from datetime import datetime

from recommender.timeutil import utcnow

from sqlmodel import Field, SQLModel


class Evaluation(SQLModel, table=True):
    __tablename__ = "evaluation"

    id: int | None = Field(default=None, primary_key=True)

    recommendation_id: int = Field(foreign_key="recommendation.id", index=True)
    judge_model_id: str  # 例 "anthropic.claude-opus-4-6"
    judge_prompt_version: str = Field(default="judge/v1.0", index=True)

    # 5 維評分 (0-1)
    relevance_score: float = Field(ge=0, le=1)
    specificity_score: float = Field(ge=0, le=1)
    actionability_score: float = Field(ge=0, le=1)
    hallucination_score: float = Field(ge=0, le=1)
    overall_score: float = Field(ge=0, le=1, index=True)  # index 方便排名 prompt variants

    judge_reasoning: str

    # Metadata
    judge_input_tokens: int | None = None
    judge_output_tokens: int | None = None
    evaluated_at: datetime = Field(default_factory=utcnow, index=True)
