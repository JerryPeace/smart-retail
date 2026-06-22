"""Pipeline endpoints — 觸發 + 查狀態"""
from fastapi import APIRouter, BackgroundTasks

from recommender.deps import PipelineServiceDep
from recommender.schemas.pipeline import JobResponse, RunPipelineRequest

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


@router.post("/run", response_model=JobResponse, status_code=202)
async def run_pipeline(
    body: RunPipelineRequest,
    background: BackgroundTasks,
    service: PipelineServiceDep,
):
    """觸發 pipeline,立刻 return job_id;實際工作在 BackgroundTask 跑"""
    job = await service.create_job(
        customer_id=body.customer_id, brand=body.brand, month=body.month
    )
    background.add_task(service.run, job.job_id)
    return job


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, service: PipelineServiceDep):
    return await service.get_job(job_id)
