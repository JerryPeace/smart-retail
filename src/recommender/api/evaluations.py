"""Evaluation endpoints — LLM-as-judge 評分"""
from fastapi import APIRouter

from recommender.deps import EvaluationServiceDep
from recommender.schemas.public import EvaluationPublic

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.post("/{recommendation_id}", status_code=201, response_model=EvaluationPublic)
async def create_evaluation(
    recommendation_id: int, service: EvaluationServiceDep
):
    """對指定 recommendation 跑一次 LLM-as-judge 評分,寫入 evaluation 表並回傳。

    找不到 recommendation → service 拋 NotFoundError → 全域 handler 回 404;
    未預期錯誤 → 全域 handler 回 500 (細節只進 log)。router 不再自己 try/except。
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
