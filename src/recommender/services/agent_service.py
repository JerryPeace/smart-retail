"""AgentService — LangChain agent 含三大關鍵能力

POC 範圍:
  1. Guardrail           — Bedrock guardrailConfig 配置 (透過環境變數)
  2. Observability       — LangSmith 自動 tracing (環境變數啟用)
  3. Evaluation          — 跑完後觸發 LLM-as-judge (stub hook)

Prompt 來源:prompts/{module}/{version}.md 檔案 (chains/ 層載入),
不從 DB PromptVariant 讀取 (dormant,未來 A/B 實作時再注入)。

Mock mode (ANALYZER_MOCK_MODE=true) 期間,回固定 fixture,讓主鏈先跑通。
Bedrock 權限下來後切換真實作。
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
    # 對外介面
    # ====================================================================
    async def analyze(
        self,
        customer_id: str,
        dataset_s3_key: str,
    ) -> RecommendationOutput:
        """跑分析,回 RecommendationOutput。"""
        if self.mock_mode:
            return self._mock_response(customer_id)

        # === 真實 path:用 chains/ 層組好的 recommendation chain ===
        # Prompt 來源:chains/recommendation.py RECOMMENDATION_PROMPT_VERSION → prompts/*.md

        # 建 LLM (cached,含 guardrail config + LangSmith auto-trace),注入 chain
        chain = build_recommendation_chain(self._build_llm())

        # ainvoke 傳 dict,key 對應 chain 內 template 的 {customer_id} / {dataset_s3_key}
        result: RecommendationOutput = await chain.ainvoke(
            {"customer_id": customer_id, "dataset_s3_key": dataset_s3_key}
        )
        return result

    # ====================================================================
    # 1. Guardrail (Bedrock 內建)
    # ====================================================================
    def _guardrail_config(self) -> dict | None:
        """產 Bedrock guardrailConfig (放進 ChatBedrockConverse 的 additional_model_request_fields).

        settings.bedrock_guardrail_id 未設 → 回 None (guardrail 不啟用)。
        settings.bedrock_guardrail_version 未設 → fallback "DRAFT"。
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
        """取 (process 層級快取的) ChatBedrockConverse — 見 recommender/llm.py。

        認證:boto3 自動讀 AWS_PROFILE / AWS_BEARER_TOKEN_BEDROCK / 標準 key。
        Observability:LangSmith 透過環境變數自動啟用。
        Guardrail:透過 additional_model_request_fields 注入 guardrailConfig (若有設定)。

        快取放 module-level (非 self),因為 service 每 request new 一個,
        instance cache 形同虛設 (review #3)。dict 不可 hash,故 guardrail 轉 tuple。
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
    # 3. Evaluation hook (LLM-as-judge,異步觸發)
    # ====================================================================
    async def trigger_evaluation(self, recommendation_id: int) -> None:
        """跑完 analyze 後在 background 觸發 judge 評分.

        TODO: 實作:
            evaluator = EvaluationService(rec_repo, eval_repo)
            await evaluator.evaluate(recommendation_id)
        """
        pass  # POC stub

    # ====================================================================
    # Mock response (POC 第一週用)
    # ====================================================================
    def _mock_response(self, customer_id: str) -> RecommendationOutput:
        return RecommendationOutput(
            customer_segment="high-value",
            recommended_products=[
                RecommendedProduct(
                    sku="P3C001",
                    product_name="iPhone 16 Pro Max 256GB",
                    reason=(
                        f"客戶 {customer_id} 過去購買多項 Apple 生態產品,"
                        "對最新 iPhone 有高度興趣"
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
