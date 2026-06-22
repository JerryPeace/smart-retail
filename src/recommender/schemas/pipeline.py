"""Pipeline API DTOs"""
from datetime import datetime

from pydantic import BaseModel, Field

from recommender.models.job import JobStatus


class RunPipelineRequest(BaseModel):
    customer_id: str = Field(..., min_length=1)
    brand: str = Field(..., min_length=1)
    month: str = Field(..., pattern=r"^\d{4}-\d{2}$")  # "2026-05"


class JobResponse(BaseModel):
    job_id: int
    status: JobStatus
    customer_id: str
    brand: str
    month: str
    error: str | None = None
    recommendation_id: int | None = None
    created_at: datetime
    updated_at: datetime
