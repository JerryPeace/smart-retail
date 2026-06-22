"""SQLModel ORM models — 資料庫表定義"""
from recommender.models.evaluation import Evaluation
from recommender.models.job import JobStatus, PipelineJob
from recommender.models.prompt_variant import PromptVariant
from recommender.models.recommendation import HubSpotSyncStatus, Recommendation

__all__ = [
    "JobStatus",
    "PipelineJob",
    "HubSpotSyncStatus",
    "Recommendation",
    "PromptVariant",
    "Evaluation",
]
