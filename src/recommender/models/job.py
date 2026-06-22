"""PipelineJob — 追蹤每次 pipeline 執行的狀態 + cleaning 統計"""
from datetime import datetime

from recommender.timeutil import utcnow
from enum import Enum

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class JobStatus(str, Enum):
    queued = "queued"
    cleaning = "cleaning"
    merging = "merging"
    analyzing = "analyzing"
    saving = "saving"
    evaluating = "evaluating"
    done = "done"
    failed = "failed"


class PipelineJob(SQLModel, table=True):
    __tablename__ = "pipeline_job"

    id: int | None = Field(default=None, primary_key=True)
    customer_id: str = Field(index=True)
    brand: str
    month: str  # 格式 "2026-05"

    status: JobStatus = Field(default=JobStatus.queued, index=True)
    error: str | None = None
    recommendation_id: int | None = Field(default=None, foreign_key="recommendation.id")

    # === Cleaning 統計 (ETL 跑完填) ===
    rows_input: int | None = None
    rows_output: int | None = None
    rows_failed: int | None = None
    cleaning_report: dict | None = Field(default=None, sa_column=Column(JSON))

    # === S3 keys (provenance) ===
    raw_keys: list[str] | None = Field(default=None, sa_column=Column(JSON))
    cleaned_dataset_key: str | None = None  # Merger 產出位置

    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)
