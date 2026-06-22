"""Repositories — DB access layer (pure CRUD, no business logic)."""
from recommender.repositories.evaluation_repo import EvaluationRepository
from recommender.repositories.job_repo import JobRepository
from recommender.repositories.prompt_variant_repo import PromptVariantRepository
from recommender.repositories.recommendation_repo import RecommendationRepository

__all__ = [
    "JobRepository",
    "RecommendationRepository",
    "PromptVariantRepository",
    "EvaluationRepository",
]
