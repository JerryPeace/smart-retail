"""Evaluation endpoints — LLM-as-judge scoring."""
from fastapi import APIRouter

from recommender.deps import EvaluationServiceDep
from recommender.schemas.public import EvaluationPublic

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.post("/{recommendation_id}", status_code=201, response_model=EvaluationPublic)
async def create_evaluation(
    recommendation_id: int, service: EvaluationServiceDep
):
    """Run one LLM-as-judge scoring for the given recommendation, write it to the evaluation table, and return it.

    Recommendation not found → service raises NotFoundError → global handler returns 404;
    unexpected error → global handler returns 500 (details only go to the log). The router no longer does its own try/except.
    """
    return await service.evaluate(recommendation_id)


@router.get("/{eval_id}", response_model=EvaluationPublic)
async def get_evaluation(eval_id: int, service: EvaluationServiceDep):
    return await service.get(eval_id)


@router.get("/by-recommendation/{recommendation_id}", response_model=list[EvaluationPublic])
async def list_by_recommendation(
    recommendation_id: int, service: EvaluationServiceDep
):
    return await service.list_by_recommendation(recommendation_id)
