"""LLM 結構化輸出 schema — agent 產出的推薦報告契約

這份 schema 同時:
  1. 給 LangChain `with_structured_output()` 當 LLM 輸出契約
  2. 給 DB Recommendation.payload 寫入時的型別保證
  3. 給 HubSpotRenderer 讀取時的型別保證
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
    """Agent 結構化輸出 v1.0"""

    schema_version: Literal["v1.0"] = "v1.0"
    customer_segment: Literal["high-value", "mid-tier", "new", "churning"]
    recommended_products: list[RecommendedProduct] = Field(..., min_length=1, max_length=5)
    customer_insights: CustomerInsight
    confidence_score: float = Field(..., ge=0, le=1)
    generated_at: datetime = Field(default_factory=utcnow)
