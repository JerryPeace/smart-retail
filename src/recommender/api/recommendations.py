"""Recommendation endpoints — 查詢 LLM 產出"""
from fastapi import APIRouter, Query

from recommender.deps import RecommendationServiceDep
from recommender.schemas.public import RecommendationPublic

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.get("/{rec_id}", response_model=RecommendationPublic)
async def get_recommendation(rec_id: int, service: RecommendationServiceDep):
    return await service.get(rec_id)


@router.get("/by-customer/{customer_id}", response_model=list[RecommendationPublic])
async def list_by_customer(
    customer_id: str,
    service: RecommendationServiceDep,
    limit: int = Query(default=20, ge=1, le=100),  # review #4:上限 100,擋全表掃
):
    return await service.list_by_customer(customer_id, limit=limit)
