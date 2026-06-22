"""Evaluation DB access — LLM-as-judge 評分紀錄的 CRUD"""
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from recommender.models.evaluation import Evaluation
from recommender.schemas.evaluation import EvaluationOutput


class EvaluationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, eval_id: int) -> Evaluation | None:
        result = await self.session.exec(
            select(Evaluation).where(Evaluation.id == eval_id)
        )
        return result.first()

    async def create_from_judge_output(
        self,
        *,
        recommendation_id: int,
        judge_model_id: str,
        judge_prompt_version: str,
        output: EvaluationOutput,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> Evaluation:
        evaluation = Evaluation(
            recommendation_id=recommendation_id,
            judge_model_id=judge_model_id,
            judge_prompt_version=judge_prompt_version,
            relevance_score=output.relevance_score,
            specificity_score=output.specificity_score,
            actionability_score=output.actionability_score,
            hallucination_score=output.hallucination_score,
            overall_score=output.overall_score,
            judge_reasoning=output.judge_reasoning,
            judge_input_tokens=input_tokens,
            judge_output_tokens=output_tokens,
        )
        self.session.add(evaluation)
        await self.session.commit()
        await self.session.refresh(evaluation)
        return evaluation

    async def list_by_recommendation(self, recommendation_id: int) -> list[Evaluation]:
        result = await self.session.exec(
            select(Evaluation)
            .where(Evaluation.recommendation_id == recommendation_id)
            .order_by(Evaluation.evaluated_at.desc())
        )
        return list(result.all())
