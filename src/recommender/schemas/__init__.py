"""Pydantic schemas — API DTOs / LLM 結構化輸出 contract"""
from recommender.schemas.cleaning import CleaningReport, RowError
from recommender.schemas.evaluation import EvaluationOutput
from recommender.schemas.pipeline import JobResponse, RunPipelineRequest
from recommender.schemas.recommendation import (
    CustomerInsight,
    RecommendationOutput,
    RecommendedProduct,
)

__all__ = [
    "RunPipelineRequest",
    "JobResponse",
    "RecommendedProduct",
    "CustomerInsight",
    "RecommendationOutput",
    "CleaningReport",
    "RowError",
    "EvaluationOutput",
]
