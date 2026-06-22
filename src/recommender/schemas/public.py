"""對外 response schema — router 回給 client 的「白名單」欄位。

為什麼 (修 review #9):
  router 直接吐 ORM 物件 (Recommendation / Evaluation) 會把所有欄位序列化出去,
  含 hubspot_sync_error、retries、token 計數等內部欄位。改用 response_model 明確
  列出可公開欄位 —— 多的欄位 FastAPI 會自動濾掉,新增內部欄位也不會意外外洩。
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RecommendationPublic(BaseModel):
    """Recommendation 對外視圖 (隱藏 hubspot sync 內部狀態 / token 計數)。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: str
    customer_segment: str | None
    confidence_score: float | None
    payload: dict
    schema_version: str
    model_id: str
    generated_at: datetime
    pipeline_job_id: int | None


class EvaluationPublic(BaseModel):
    """Evaluation 對外視圖 (隱藏 judge token 計數)。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    recommendation_id: int
    judge_model_id: str
    judge_prompt_version: str
    relevance_score: float
    specificity_score: float
    actionability_score: float
    hallucination_score: float
    overall_score: float
    judge_reasoning: str
    evaluated_at: datetime
