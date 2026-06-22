"""Pipeline — orchestrates the full dataset → agent → save flow"""
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlmodel.ext.asyncio.session import AsyncSession

from recommender.config import settings
from recommender.db import SessionLocal
from recommender.errors import NotFoundError
from recommender.models.job import JobStatus, PipelineJob
from recommender.repositories.job_repo import JobRepository
from recommender.repositories.recommendation_repo import RecommendationRepository
from recommender.schemas.pipeline import JobResponse
from recommender.services.agent_service import AgentService
from recommender.services.dataset_service import DatasetService

logger = logging.getLogger(__name__)


class PipelineService:
    def __init__(
        self,
        dataset: DatasetService,
        agent: AgentService,
        job_repo: JobRepository,
        rec_repo: RecommendationRepository,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = SessionLocal,
    ) -> None:
        self.dataset = dataset
        self.agent = agent
        # job_repo / rec_repo are bound to a request-scoped session, only for use by
        # create_job / get_job (called within the request lifecycle). run() executes in a
        # BackgroundTask and must not use them.
        self.job_repo = job_repo
        self.rec_repo = rec_repo
        # Factory that run() uses to open its own new session (review #2)
        self._session_factory = session_factory

    async def create_job(
        self, customer_id: str, brand: str, month: str
    ) -> JobResponse:
        job = await self.job_repo.create(
            customer_id=customer_id, brand=brand, month=month
        )
        return self._to_response(job)

    async def get_job(self, job_id: int) -> JobResponse:
        """Not found → raise NotFoundError"""
        job = await self.job_repo.get(job_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found")
        return self._to_response(job)

    def _to_response(self, job: PipelineJob) -> JobResponse:
        return JobResponse(
            job_id=job.id,
            status=job.status,
            customer_id=job.customer_id,
            brand=job.brand,
            month=job.month,
            error=job.error,
            recommendation_id=job.recommendation_id,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    async def run(self, job_id: int) -> None:
        """The full pipeline, run in a BackgroundTask.

        review #2: a BackgroundTask only runs after the response has been sent, at which
        point the request-scoped session has long been closed by its async with block.
        Reusing self.job_repo (bound to the old session) would raise InvalidRequestError on
        the first await. So here we open an independent session and rebuild the repos with
        it, giving them a lifecycle tied to the background task.

        On failure, record the error status and re-raise, leaving a trace before the
        BackgroundTask swallows the exception.
        """
        async with self._session_factory() as session:
            job_repo = JobRepository(session)
            rec_repo = RecommendationRepository(session)

            job = await job_repo.get(job_id)
            if job is None:
                raise NotFoundError(f"Job {job_id} not found")

            try:
                # === Step 1: Dataset preparation (S3 raw → cleaned dataset) ===
                await job_repo.update_status(job_id, JobStatus.cleaning)
                cleaned_key, _report = await self.dataset.prepare(
                    customer_id=job.customer_id,
                    brand=job.brand,
                    month=job.month,
                )
                # TODO: write _report (rows_in/out, etc.) into PipelineJob

                # === Step 2: Agent analyze ===
                await job_repo.update_status(job_id, JobStatus.analyzing)
                agent_output = await self.agent.analyze(
                    customer_id=job.customer_id,
                    dataset_s3_key=cleaned_key,
                )

                # === Step 3: Save recommendation ===
                await job_repo.update_status(job_id, JobStatus.saving)
                rec = await rec_repo.create_from_agent_output(
                    customer_id=job.customer_id,
                    output=agent_output,
                    model_id=settings.bedrock_model_id,
                    pipeline_job_id=job_id,
                )

                # === Step 4: Trigger evaluation (async, doesn't block pipeline completion) ===
                # await self.agent.trigger_evaluation(rec.id)

                await job_repo.update_status(
                    job_id, JobStatus.done, recommendation_id=rec.id
                )

            except Exception:
                # review #6: the full traceback goes only to the log; the DB / client store only a generic message, leaking no internal details
                logger.exception("Pipeline job %s failed", job_id)
                await job_repo.update_status(
                    job_id,
                    JobStatus.failed,
                    error=f"Pipeline failed during processing (job {job_id})",
                )
                raise
