"""RecommendationService — read-only query service"""
from recommender.errors import NotFoundError
from recommender.repositories.recommendation_repo import RecommendationRepository
from recommender.schemas.public import RecommendationPublic


class RecommendationService:
    def __init__(self, rec_repo: RecommendationRepository) -> None:
        self.rec_repo = rec_repo

    async def get(self, rec_id: int) -> RecommendationPublic:
        """Not found → raise NotFoundError"""
        rec = await self.rec_repo.get(rec_id)
        if rec is None:
            raise NotFoundError(f"Recommendation {rec_id} not found")
        return RecommendationPublic.model_validate(rec)

    async def list_by_customer(
        self, customer_id: str, limit: int = 20
    ) -> list[RecommendationPublic]:
        recs = await self.rec_repo.list_by_customer(customer_id, limit=limit)
        return [RecommendationPublic.model_validate(r) for r in recs]
