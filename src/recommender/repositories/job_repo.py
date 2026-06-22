"""PipelineJob 的 DB access"""
from recommender.timeutil import utcnow

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from recommender.errors import NotFoundError
from recommender.models.job import JobStatus, PipelineJob


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, customer_id: str, brand: str, month: str) -> PipelineJob:
        job = PipelineJob(
            customer_id=customer_id,
            brand=brand,
            month=month,
            status=JobStatus.queued,
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def get(self, job_id: int) -> PipelineJob | None:
        result = await self.session.exec(
            select(PipelineJob).where(PipelineJob.id == job_id)
        )
        return result.first()

    async def update_status(
        self,
        job_id: int,
        status: JobStatus,
        *,
        error: str | None = None,
        recommendation_id: int | None = None,
    ) -> PipelineJob:
        job = await self.get(job_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found")

        job.status = status
        job.updated_at = utcnow()
        if error is not None:
            job.error = error
        if recommendation_id is not None:
            job.recommendation_id = recommendation_id

        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def list_recent(self, limit: int = 50) -> list[PipelineJob]:
        result = await self.session.exec(
            select(PipelineJob).order_by(PipelineJob.created_at.desc()).limit(limit)
        )
        return list(result.all())
