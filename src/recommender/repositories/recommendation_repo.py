"""DB access for Recommendation."""
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from recommender.models.recommendation import HubSpotSyncStatus, Recommendation
from recommender.schemas.recommendation import RecommendationOutput


class RecommendationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, rec_id: int) -> Recommendation | None:
        result = await self.session.exec(
            select(Recommendation).where(Recommendation.id == rec_id)
        )
        return result.first()

    async def create_from_agent_output(
        self,
        *,
        customer_id: str,
        output: RecommendationOutput,
        model_id: str,
        pipeline_job_id: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> Recommendation:
        """Dump the Pydantic agent output directly into JSONB, while also extracting hot columns."""
        rec = Recommendation(
            customer_id=customer_id,
            customer_segment=output.customer_segment,
            confidence_score=output.confidence_score,
            payload=output.model_dump(mode="json"),
            schema_version=output.schema_version,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            pipeline_job_id=pipeline_job_id,
            hubspot_sync_status=HubSpotSyncStatus.pending,
        )
        self.session.add(rec)
        await self.session.commit()
        await self.session.refresh(rec)
        return rec

    async def list_by_customer(
        self, customer_id: str, limit: int = 20
    ) -> list[Recommendation]:
        result = await self.session.exec(
            select(Recommendation)
            .where(Recommendation.customer_id == customer_id)
            .order_by(Recommendation.generated_at.desc())
            .limit(limit)
        )
        return list(result.all())

    async def list_pending_hubspot_sync(self, limit: int = 50) -> list[Recommendation]:
        result = await self.session.exec(
            select(Recommendation)
            .where(Recommendation.hubspot_sync_status == HubSpotSyncStatus.pending)
            .order_by(Recommendation.generated_at.asc())
            .limit(limit)
        )
        return list(result.all())
