"""DI providers — 集中管理所有依賴注入

FastAPI 用 `Depends()` 機制,這個檔是唯一管 wiring 的地方。
NestJS 對應概念: Module + Provider。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from opensearchpy import AsyncOpenSearch

from recommender.config import settings
from recommender.db import get_session
from recommender.repositories.evaluation_repo import EvaluationRepository
from recommender.repositories.job_repo import JobRepository
from recommender.repositories.prompt_variant_repo import PromptVariantRepository
from recommender.repositories.recommendation_repo import RecommendationRepository
from search_engine.client import get_opensearch_client
from search_engine.repository import SearchRepository
from search_engine.service import SearchService
from recommender.services.agent_service import AgentService
from recommender.services.dataset_service import DatasetService
from recommender.services.evaluation_service import EvaluationService
from recommender.services.pipeline_service import PipelineService
from recommender.services.recommendation_service import RecommendationService
from recommender.services.s3_service import S3Service

# === Session ===
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# === Repositories ===
def get_job_repo(session: SessionDep) -> JobRepository:
    return JobRepository(session)


def get_recommendation_repo(session: SessionDep) -> RecommendationRepository:
    return RecommendationRepository(session)


def get_prompt_variant_repo(session: SessionDep) -> PromptVariantRepository:
    return PromptVariantRepository(session)


def get_evaluation_repo(session: SessionDep) -> EvaluationRepository:
    return EvaluationRepository(session)


JobRepoDep = Annotated[JobRepository, Depends(get_job_repo)]
RecommendationRepoDep = Annotated[RecommendationRepository, Depends(get_recommendation_repo)]
PromptVariantRepoDep = Annotated[PromptVariantRepository, Depends(get_prompt_variant_repo)]
EvaluationRepoDep = Annotated[EvaluationRepository, Depends(get_evaluation_repo)]


def get_recommendation_service(
    rec_repo: RecommendationRepoDep,
) -> RecommendationService:
    return RecommendationService(rec_repo)


RecommendationServiceDep = Annotated[RecommendationService, Depends(get_recommendation_service)]


# === Services ===
def get_s3_service() -> S3Service:
    return S3Service()


S3ServiceDep = Annotated[S3Service, Depends(get_s3_service)]


def get_dataset_service(s3: S3ServiceDep) -> DatasetService:
    return DatasetService(s3)


def get_agent_service() -> AgentService:
    return AgentService()


DatasetServiceDep = Annotated[DatasetService, Depends(get_dataset_service)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]


def get_pipeline_service(
    dataset: DatasetServiceDep,
    agent: AgentServiceDep,
    job_repo: JobRepoDep,
    rec_repo: RecommendationRepoDep,
) -> PipelineService:
    return PipelineService(dataset, agent, job_repo, rec_repo)


PipelineServiceDep = Annotated[PipelineService, Depends(get_pipeline_service)]


def get_evaluation_service(
    rec_repo: RecommendationRepoDep,
    eval_repo: EvaluationRepoDep,
) -> EvaluationService:
    return EvaluationService(rec_repo, eval_repo)


EvaluationServiceDep = Annotated[EvaluationService, Depends(get_evaluation_service)]


# === Search (OpenSearch 領域模組，唯一 wiring 點) ===

OSClientDep = Annotated["AsyncOpenSearch", Depends(get_opensearch_client)]


def get_search_repository(os_client: OSClientDep) -> SearchRepository:
    """建 SearchRepository，注入 os_client 與 opensearch_index。"""
    return SearchRepository(os_client, index=settings.opensearch_index)


SearchRepoDep = Annotated[SearchRepository, Depends(get_search_repository)]


def get_search_service(repo: SearchRepoDep) -> SearchService:
    """建 SearchService，注入 SearchRepository。"""
    return SearchService(repo)


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
