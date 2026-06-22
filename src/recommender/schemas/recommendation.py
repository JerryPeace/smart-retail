"""LLM structured-output schema — the contract for the recommendation report the agent produces.

This schema simultaneously serves as:
  1. The LLM output contract for LangChain `with_structured_output()`
  2. The type guarantee when writing to DB Recommendation.payload
  3. The type guarantee when HubSpotRenderer reads it
"""
from datetime import datetime

from recommender.timeutil import utcnow
from typing import Literal

from pydantic import BaseModel, Field


class RecommendedProduct(BaseModel):
    sku: str
    product_name: str
    reason: str = Field(..., min_length=10, max_length=500)
    confidence: float = Field(..., ge=0, le=1)


class CustomerInsight(BaseModel):
    interests: list[str]
    purchase_pattern: str
    next_best_action: str


class RecommendationOutput(BaseModel):
    """Agent structured output v1.0."""

    schema_version: Literal["v1.0"] = "v1.0"
    customer_segment: Literal["high-value", "mid-tier", "new", "churning"]
    recommended_products: list[RecommendedProduct] = Field(..., min_length=1, max_length=5)
    customer_insights: CustomerInsight
    confidence_score: float = Field(..., ge=0, le=1)
    generated_at: datetime = Field(default_factory=utcnow)
