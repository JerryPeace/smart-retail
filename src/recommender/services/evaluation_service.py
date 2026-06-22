"""EvaluationService — LLM-as-judge service

Responsibilities:
  1. Load the recommendation
  2. Load the judge prompt template from file and inject the content
  3. Call the judge LLM (Opus 4.6 by default) to get a structured score
  4. Write to the evaluation table

In mock mode (ANALYZER_MOCK_MODE=true) it returns fixed scores directly, without calling Bedrock.
"""
from __future__ import annotations

from recommender.chains.judge import JUDGE_PROMPT_VERSION, build_judge_chain
from recommender.config import settings
from recommender.errors import NotFoundError
from recommender.llm import get_bedrock_llm
from recommender.repositories.evaluation_repo import EvaluationRepository
from recommender.repositories.recommendation_repo import RecommendationRepository
from recommender.schemas.evaluation import EvaluationOutput
from recommender.schemas.public import EvaluationPublic
from recommender.schemas.recommendation import RecommendationOutput
from recommender.timeutil import utcnow


class EvaluationService:
    def __init__(
        self,
        rec_repo: RecommendationRepository,
        eval_repo: EvaluationRepository,
    ) -> None:
        self.rec_repo = rec_repo
        self.eval_repo = eval_repo
        self.mock_mode = settings.analyzer_mock_mode

    async def evaluate(self, recommendation_id: int) -> EvaluationPublic:
        rec = await self.rec_repo.get(recommendation_id)
        if rec is None:
            raise NotFoundError(f"Recommendation {recommendation_id} not found")

        output = RecommendationOutput.model_validate(rec.payload)

        if self.mock_mode:
            judge_output = self._mock_judge_output()
            input_tokens = None
            output_tokens = None
            judge_model_id = "mock"
        else:
            judge_output, input_tokens, output_tokens = await self._call_judge(rec.customer_id, output)
            judge_model_id = settings.bedrock_judge_model_id

        evaluation = await self.eval_repo.create_from_judge_output(
            recommendation_id=recommendation_id,
            judge_model_id=judge_model_id,
            judge_prompt_version=JUDGE_PROMPT_VERSION,
            output=judge_output,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return EvaluationPublic.model_validate(evaluation)

    async def get(self, eval_id: int) -> EvaluationPublic:
        """Not found → raise NotFoundError"""
        evaluation = await self.eval_repo.get(eval_id)
        if evaluation is None:
            raise NotFoundError(f"Evaluation {eval_id} not found")
        return EvaluationPublic.model_validate(evaluation)

    async def list_by_recommendation(
        self, recommendation_id: int
    ) -> list[EvaluationPublic]:
        evaluations = await self.eval_repo.list_by_recommendation(recommendation_id)
        return [EvaluationPublic.model_validate(e) for e in evaluations]

    # ====================================================================
    # Real path: call the Bedrock judge LLM
    # ====================================================================
    async def _call_judge(
        self, customer_id: str, output: RecommendationOutput
    ) -> tuple[EvaluationOutput, int | None, int | None]:
        # Use the judge chain assembled in the chains/ layer (include_raw=True → dict{parsed, raw})
        chain = build_judge_chain(self._build_llm())

        # The variables are already aggregated by downstream ETL (products_text, etc.); here we only inject them
        result = await chain.ainvoke(self._build_inputs(customer_id, output))
        parsed: EvaluationOutput = result["parsed"]
        raw_msg = result["raw"]

        usage = getattr(raw_msg, "usage_metadata", None) or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        return parsed, input_tokens, output_tokens

    def _build_llm(self):
        """Get the (process-level cached) judge LLM — see recommender/llm.py (review #3).

        temperature=0.0: the judge must be stable and reproducible, not creative.
        """
        return get_bedrock_llm(
            model=settings.bedrock_judge_model_id,
            region=settings.bedrock_region,
            temperature=0.0,
            max_tokens=2048,
        )

    def _build_inputs(self, customer_id: str, output: RecommendationOutput) -> dict:
        """Aggregate RecommendationOutput into the prompt template's injection variables (pure Python ETL).

        products_text is assembled here algorithmically; the LLM only receives the already-aggregated
        string — in line with "ETL First, LLM Last": don't hand the raw structure to the LLM and ask
        it to organize it itself.
        The returned dict keys correspond to the {variables} in judge/v1.0.md.
        """
        products_text = "\n".join(
            f"- [{p.sku}] {p.product_name} (信心 {p.confidence:.2f})\n  理由: {p.reason}"
            for p in output.recommended_products
        )

        return {
            "customer_id": customer_id,
            "customer_segment": output.customer_segment,
            "confidence_score": f"{output.confidence_score:.2f}",
            "products_text": products_text,
            "interests": ", ".join(output.customer_insights.interests),
            "purchase_pattern": output.customer_insights.purchase_pattern,
            "next_best_action": output.customer_insights.next_best_action,
        }

    # ====================================================================
    # Mock path: used when ANALYZER_MOCK_MODE=true
    # ====================================================================
    def _mock_judge_output(self) -> EvaluationOutput:
        return EvaluationOutput(
            relevance_score=0.78,
            specificity_score=0.62,
            actionability_score=0.71,
            hallucination_score=0.85,
            overall_score=0.74,
            judge_reasoning="(mock) 推薦商品品類與經銷商過往交易吻合,但理由偏通用、缺具體進貨量建議。",
            evaluated_at=utcnow(),
        )
