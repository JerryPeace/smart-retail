"""Public-facing response schemas — the "whitelist" of fields the router returns to the client.

Why (fixes review #9):
  A router that returns ORM objects directly (Recommendation / Evaluation) serializes every field,
  including internal fields like hubspot_sync_error, retries, and token counts. Using response_model to explicitly
  list the publishable fields — FastAPI automatically filters out the extras, so newly added internal fields won't accidentally leak.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RecommendationPublic(BaseModel):
    """Public view of Recommendation (hides hubspot sync internal status / token counts)."""

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
    """Public view of Evaluation (hides judge token counts)."""

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
