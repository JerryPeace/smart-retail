"""AgentService — LangChain agent with three key capabilities

POC scope:
  1. Guardrail           — Bedrock guardrailConfig setup (via environment variables)
  2. Observability       — LangSmith automatic tracing (enabled via environment variables)
  3. Evaluation          — triggers LLM-as-judge after a run (stub hook)

Prompt source: prompts/{module}/{version}.md files (loaded by the chains/ layer),
not read from the DB PromptVariant (dormant; to be injected later when A/B is implemented).

During mock mode (ANALYZER_MOCK_MODE=true), it returns a fixed fixture so the main chain
can run end-to-end first. Switch to the real implementation once Bedrock access is granted.
"""
from recommender.chains.recommendation import build_recommendation_chain
from recommender.config import settings
from recommender.llm import get_bedrock_llm
from recommender.schemas.recommendation import (
    CustomerInsight,
    RecommendationOutput,
    RecommendedProduct,
)
from recommender.timeutil import utcnow


class AgentService:
    def __init__(self) -> None:
        self.mock_mode = settings.analyzer_mock_mode

    # ====================================================================
    # Public interface
    # ====================================================================
    async def analyze(
        self,
        customer_id: str,
        dataset_s3_key: str,
        month: str = "",
    ) -> RecommendationOutput:
        """Run the analysis and return a RecommendationOutput.

        month ("YYYY-MM") grounds the seasonal/peak-season reasoning in the prompt.
        """
        if self.mock_mode:
            return self._mock_response(customer_id, month)

        # === Real path: use the recommendation chain assembled in the chains/ layer ===
        # Prompt source: chains/recommendation.py RECOMMENDATION_PROMPT_VERSION → prompts/*.md

        # Build the LLM (cached, with guardrail config + LangSmith auto-trace) and inject it into the chain
        chain = build_recommendation_chain(self._build_llm())

        # ainvoke takes a dict whose keys map to the chain template's {customer_id} / {month} / {dataset_s3_key}
        result: RecommendationOutput = await chain.ainvoke(
            {"customer_id": customer_id, "month": month, "dataset_s3_key": dataset_s3_key}
        )
        return result

    # ====================================================================
    # 1. Guardrail (Bedrock 內建)
    # ====================================================================
    def _guardrail_config(self) -> dict | None:
        """Build the Bedrock guardrailConfig (passed into ChatBedrockConverse's additional_model_request_fields).

        settings.bedrock_guardrail_id not set → return None (guardrail disabled).
        settings.bedrock_guardrail_version not set → fall back to "DRAFT".
        """
        if not settings.bedrock_guardrail_id:
            return None
        return {
            "guardrailIdentifier": settings.bedrock_guardrail_id,
            "guardrailVersion": settings.bedrock_guardrail_version or "DRAFT",
            "trace": "enabled",
        }

    # ====================================================================
    # 2. Build LLM (含 observability)
    # ====================================================================
    def _build_llm(self):
        """Get the (process-level cached) ChatBedrockConverse — see recommender/llm.py.

        Auth: boto3 automatically reads AWS_PROFILE / AWS_BEARER_TOKEN_BEDROCK / standard keys.
        Observability: LangSmith is enabled automatically via environment variables.
        Guardrail: guardrailConfig is injected via additional_model_request_fields (if configured).

        The cache lives at module level (not on self), because a new service is created per
        request, making an instance cache useless (review #3). A dict isn't hashable, so the
        guardrail is converted to a tuple.
        """
        gr = self._guardrail_config()
        guardrail_items = tuple(sorted(gr.items())) if gr else None
        return get_bedrock_llm(
            model=settings.bedrock_model_id,
            region=settings.bedrock_region,
            temperature=0.3,
            max_tokens=4096,
            guardrail_items=guardrail_items,
        )

    # ====================================================================
    # 3. Evaluation hook (LLM-as-judge, triggered asynchronously)
    # ====================================================================
    async def trigger_evaluation(self, recommendation_id: int) -> None:
        """Trigger judge scoring in the background after analyze completes.

        TODO: implement:
            evaluator = EvaluationService(rec_repo, eval_repo)
            await evaluator.evaluate(recommendation_id)
        """
        pass  # POC stub

    # ====================================================================
    # Mock response (used in the first week of the POC)
    # ====================================================================
    def _mock_response(self, customer_id: str, month: str = "") -> RecommendationOutput:
        season = f"{month} " if month else ""
        return RecommendationOutput(
            customer_segment="high-value",
            recommended_products=[
                RecommendedProduct(
                    sku="P3C001",
                    product_name="iPhone 16 Pro Max 256GB",
                    reason=(
                        f"客戶 {customer_id} 過去購買多項 Apple 生態產品,對最新 iPhone 有高度興趣;"
                        f"{season}適逢通訊換機檔期,建議提前備貨主推"
                    ),
                    confidence=0.91,
                ),
                RecommendedProduct(
                    sku="P3C003",
                    product_name="AirPods Pro 3",
                    reason="搭配主推商品的高轉換配件,單價低且符合既有購買模式",
                    confidence=0.82,
                ),
            ],
            customer_insights=CustomerInsight(
                interests=["Apple 生態", "高階 3C"],
                purchase_pattern="季度更新型,每 3-6 月有大筆消費",
                next_best_action="於下次新品發表後 7 日內主動接觸",
            ),
            confidence_score=0.87,
            generated_at=utcnow(),
        )
